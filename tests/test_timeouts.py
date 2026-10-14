from tests.native_async import native, native_runnable, async_stub
import types
from uuid import uuid4
import httpx
import openai
import pytest
from agent_service import execution
from agent_service import model_calls
from agent_service import manifest as manifest_module
from agent_service import worker
from agent_service.timeouts import CallTimeouts, configured_timeouts, timeouts_for_manifest
from agent_service.tooling import RunExecutionState as ToolExecutionContext

def test_timeout_configuration_and_manifest_fallback(monkeypatch):
    monkeypatch.delenv('AGENT_MODEL_TIMEOUT_SECONDS', raising=False)
    monkeypatch.delenv('AGENT_RAG_TIMEOUT_SECONDS', raising=False)
    assert configured_timeouts() == CallTimeouts(120, 120)
    monkeypatch.setenv('AGENT_MODEL_TIMEOUT_SECONDS', '13.5')
    monkeypatch.setenv('AGENT_RAG_TIMEOUT_SECONDS', '17')
    assert configured_timeouts() == CallTimeouts(13.5, 17)
    manifest = manifest_module.execution_manifest()
    assert manifest['limits']['model_timeout_seconds'] == 13.5
    assert manifest['limits']['rag_timeout_seconds'] == 17
    monkeypatch.setenv('AGENT_MODEL_TIMEOUT_SECONDS', '29')
    monkeypatch.setenv('AGENT_RAG_TIMEOUT_SECONDS', '31')
    assert timeouts_for_manifest(manifest) == CallTimeouts(13.5, 17)
    assert timeouts_for_manifest({'limits': {'model_timeout_seconds': 13.5}}) == CallTimeouts(13.5, 31)
    assert timeouts_for_manifest({}) == CallTimeouts(29, 31)
    with pytest.raises(ValueError, match='manifest model_timeout_seconds'):
        timeouts_for_manifest({'limits': {'model_timeout_seconds': True}})

@pytest.mark.parametrize('bad', ['0', '-1', 'nan', 'inf', '-inf', 'not-a-number', ''])
@pytest.mark.parametrize('name', ['AGENT_MODEL_TIMEOUT_SECONDS', 'AGENT_RAG_TIMEOUT_SECONDS'])
def test_timeout_configuration_rejects_invalid_values(monkeypatch, name, bad):
    monkeypatch.setenv(name, bad)
    with pytest.raises(ValueError, match=name):
        configured_timeouts()

def test_worker_rejects_invalid_timeout_before_starting(monkeypatch):
    monkeypatch.setenv('AGENT_DATABASE_URL', 'postgresql://user:secret@localhost/test')
    monkeypatch.setenv('AGENT_MODEL_URL', 'http://model.test')
    monkeypatch.setenv('AGENT_MODEL', 'test-model')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 's' * 43)
    monkeypatch.setenv('AGENT_MODEL_TIMEOUT_SECONDS', 'nan')
    with pytest.raises(ValueError, match='AGENT_MODEL_TIMEOUT_SECONDS'):
        worker.main()

async def test_model_factory_uses_managed_async_client_and_disables_retries(monkeypatch):
    from agent_service import model_adapter
    from execution.persistence import resources
    captured={}
    monkeypatch.setenv('AGENT_MODEL_URL','http://model.test')
    monkeypatch.setenv('AGENT_MODEL','test-model')
    monkeypatch.setattr(model_adapter,'ChatOpenAI',lambda **kwargs:captured.update(kwargs) or kwargs)
    model_adapter.model()
    assert captured['max_retries']==0
    assert captured['base_url']=='http://model.test/v1'
    assert captured['root_async_client']._client is resources().http.sdk_client('agent-model')
    with pytest.raises(RuntimeError):captured['client'].create

@pytest.mark.parametrize('exception', [lambda: openai.APITimeoutError(request=httpx.Request('POST', 'http://model.test/v1/chat/completions')), lambda: httpx.ReadTimeout('timeout')])
async def test_react_model_timeout_is_settled_once(monkeypatch, exception):
    from tests.helpers import insert_run
    from agent_service.runtime import claim_run
    run_id=insert_run(); lease_token=(await claim_run())['lease_token']; call_id=uuid4()
    context = ToolExecutionContext(run_id, lease_token)
    settled = []
    monkeypatch.setattr(model_calls, 'reserve_model', lambda *_args, **_kwargs: call_id)
    monkeypatch.setattr(model_calls, 'settle_model', lambda *args, **kwargs: settled.append((args, kwargs)) or True)
    request = types.SimpleNamespace(model=None, runtime=types.SimpleNamespace(context=context), state={'messages': []}, messages=[], system_message=None, tools=[], override=lambda **kwargs:request)
    with pytest.raises(execution.ModelTimeoutError):
        await native(execution.budgeted_model.awrap_model_call, request, lambda _request: (_ for _ in ()).throw(exception()))
    assert len(settled) == 1
    assert settled[0][0][3] == 'failed'
    assert settled[0][1]['error_code'] == 'MODEL_TIMEOUT'

async def test_structured_model_timeout_is_settled_once(monkeypatch):
    from tests.helpers import insert_run
    from agent_service.runtime import claim_run
    run_id=insert_run(); lease_token=(await claim_run())['lease_token']; call_id=uuid4()
    context = ToolExecutionContext(run_id, lease_token)
    settled = []
    monkeypatch.setattr(model_calls, 'reserve_model', lambda *_args, **_kwargs: call_id)
    monkeypatch.setattr(model_calls, 'settle_model', lambda *args, **kwargs: settled.append((args, kwargs)) or True)
    from tests.native_async import native_model
    native_model(types.SimpleNamespace)
    llm = types.SimpleNamespace(with_structured_output=lambda _schema: types.SimpleNamespace(ainvoke=async_stub(lambda _messages: (_ for _ in ()).throw(httpx.ReadTimeout('timeout')))))
    with pytest.raises(execution.ModelTimeoutError):
        await native(execution.invoke_structured, llm, execution.Plan, [], context, 'planner')
    assert len(settled) == 1
    assert settled[0][1]['error_code'] == 'MODEL_TIMEOUT'

async def test_model_authentication_remains_distinct_and_lost_lease_is_not_auth_failure(monkeypatch):
    from tests.helpers import insert_run
    from agent_service.runtime import claim_run
    run_id=insert_run();context=ToolExecutionContext(run_id,(await claim_run())['lease_token'])
    call_id = uuid4()
    settled = []
    monkeypatch.setattr(model_calls, 'reserve_model', lambda *args, **kwargs: call_id)
    monkeypatch.setattr(model_calls, 'settle_model', lambda *args, **kwargs: settled.append(kwargs) or True)
    request = types.SimpleNamespace(model=None, runtime=types.SimpleNamespace(context=context), state={}, messages=[], system_message=None, tools=[], override=lambda **kwargs:request)

    def failure(_):
        raise openai.AuthenticationError('rejected', response=httpx.Response(401, request=httpx.Request('POST', 'http://model.test')), body={})
    with pytest.raises(execution.ModelAuthError):
        await native(execution.budgeted_model.awrap_model_call, request, failure)
    assert settled == [{'error_code': 'MODEL_AUTH_FAILED'}]
    monkeypatch.setattr(model_calls, 'settle_model', lambda *args, **kwargs: False)
    with pytest.raises(PermissionError):
        await native(execution.budgeted_model.awrap_model_call, request, failure)



@pytest.mark.parametrize('failure',['timeout','authentication','other'])
async def test_memory_failure_after_lost_renewal_is_not_model_failure(monkeypatch,failure):
    import asyncio
    from execution.scope import attempt
    from execution.contracts import TaskRef,TaskKind
    from agent_service import memory
    stopped=asyncio.Event();stopped.set()
    job={'id':uuid4(),'lease_token':uuid4()}
    with attempt(TaskRef(job['id'],TaskKind.MEMORY),job['lease_token'],stopped):
        with pytest.raises(PermissionError):await memory.memory_structured(memory.SummaryDraft,[])
