"""Fourth-stage call/control boundaries with scripted IO and isolated PostgreSQL."""
import asyncio
import json
import threading
from uuid import uuid4
import httpx
import psycopg
import pytest
from execution.calls import CallContext, CallRunner, ProviderReply
from execution.call_store import AgentCallStore
from execution.contracts import TaskRef, TaskKind
from execution.errors import ExecutionRevoked, select_failure
from execution.infra.types import OperationDeadline, WorkOwner, ResourceClosed, WorkStopped, CpuPoolReset
from execution.persistence import resources, bind, call_scope, deadline, PersistenceOutcomeUnknown, Applied, transaction
from execution.scope import attempt, current_attempt, local_authority
from execution.providers import ProviderFailure, embedding_vectors
from tests.helpers import insert_run


def call():
    return CallContext(TaskRef(uuid4(), TaskKind.AGENT), uuid4(), OperationDeadline.after(2))


@pytest.mark.parametrize('phase', ['reservation', 'settlement', 'failure_settlement'])
async def test_unknown_persistence_never_dispatches_or_resettles(phase):
    events = []
    async def reserve(identity):
        events.append('reserve')
        if phase == 'reservation': raise PersistenceOutcomeUnknown()
        return Applied(identity)
    async def invoke(context):
        events.append('send')
        if phase == 'failure_settlement': raise ValueError('invalid provider output')
        return ProviderReply('value')
    async def settle(*args, **kwargs):
        events.append('settle')
        raise PersistenceOutcomeUnknown()
    with pytest.raises(PersistenceOutcomeUnknown):
        await CallRunner().run(call(), reserve=reserve, invoke=invoke, settle=settle,
            failure=lambda error: ('INVALID', {}, error))
    assert events == (['reserve'] if phase == 'reservation' else ['reserve', 'send', 'settle'])


async def test_existing_reservation_does_not_authorize_external_replay():
    from agent_service.runtime import claim_run
    rid = insert_run(); job = await claim_run(); ledger = AgentCallStore(resources())
    context = CallContext(TaskRef(rid, TaskKind.AGENT), job['lease_token'], OperationDeadline.after(2))
    async def reserve(identity):
        return await ledger.reserve_model(rid, job['lease_token'], 'react', 'mock', call_id=identity)
    assert isinstance(await reserve(context.id), Applied)
    async def forbidden(*args, **kwargs): pytest.fail('Existing call must not be dispatched or re-settled')
    with pytest.raises(PersistenceOutcomeUnknown):
        await CallRunner().run(context, reserve=reserve, invoke=forbidden, settle=forbidden, failure=forbidden)
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone()
    assert row['model_calls_used'] == 1


@pytest.mark.parametrize('error', [ExecutionRevoked(), ResourceClosed(), WorkStopped(), psycopg.OperationalError()])
async def test_control_signal_is_never_settled_as_provider_failure(error):
    async def reserve(identity): return Applied(identity)
    async def invoke(context): raise error
    async def forbidden(*args, **kwargs): pytest.fail('Control is not a provider failure')
    with pytest.raises(type(error)):
        await CallRunner().run(call(), reserve=reserve, invoke=invoke, settle=forbidden, failure=forbidden)


async def test_file_permission_failure_is_a_business_error():
    seen = []
    async def reserve(identity): return Applied(identity)
    async def invoke(context): raise PermissionError('File cannot be read')
    async def settle(identity, status, **metadata): seen.append((status, metadata)); return Applied(identity)
    from agent_service.tooling import tool_failure, ToolCallFailed
    with pytest.raises(ToolCallFailed):
        await CallRunner().run(call(), reserve=reserve, invoke=invoke, settle=settle, failure=tool_failure)
    assert seen[0][0] == 'failed' and seen[0][1]['error_code'] == 'TOOL_CALL_FAILED'


async def test_nested_attempt_keeps_supervisor_stop_and_deadline(monkeypatch):
    from agent_service.execution import execute_run
    from execution.inputs import prepare_inputs
    from execution import inputs
    context = call(); stopped = asyncio.Event(); stopped.set()
    async def inspect_inputs(task, token):
        assert current_attempt().stopped is stopped
        local_authority(task, token)
        pytest.fail('Stopped attempt advanced')
    monkeypatch.setattr(inputs, 'prepare_inputs', inspect_inputs)
    with attempt(context.task, context.token, stopped, deadline=context.deadline):
        with pytest.raises(ExecutionRevoked): await execute_run(context.task.id, context.token)
        with pytest.raises(ExecutionRevoked):
            with attempt(context.task, uuid4()): pass
        with pytest.raises(ExecutionRevoked):
            with attempt(context.task, context.token, asyncio.Event()): pass
        with pytest.raises(ExecutionRevoked):
            with attempt(context.task, context.token, deadline=OperationDeadline.after(100)): pass
    assert current_attempt() is None


async def test_nested_deadline_and_concurrent_contexts_are_isolated():
    runtime = resources(); started = asyncio.Event(); release = asyncio.Event()
    async def bounded(seconds):
        outer = OperationDeadline.after(seconds)
        with bind(runtime, deadline=OperationDeadline.after(100)), call_scope(outer):
            started.set(); await release.wait()
            return deadline(500).when, outer.when
    first = asyncio.create_task(bounded(2)); await started.wait(); started.clear()
    second = asyncio.create_task(bounded(10)); await started.wait(); release.set()
    a, b = await asyncio.gather(first, second)
    assert a[0] == a[1] and b[0] == b[1] and b[0] > a[0]


async def test_request_timeout_can_be_settled_after_call_deadline():
    import execution.calls as calls
    context = call(); sent = asyncio.Event(); finalized = []
    async def reserve(identity): return Applied(identity)
    async def invoke(current): sent.set(); raise TimeoutError()
    async def settle(identity, status, **metadata):
        from execution.persistence import current_call_deadline
        assert current_call_deadline() is None
        finalized.append(status); return Applied(identity)
    from agent_service.model_calls import failure, ModelTimeoutError
    with pytest.raises(ModelTimeoutError):
        await calls.CallRunner().run(context, reserve=reserve, invoke=invoke, settle=settle, failure=failure)
    assert sent.is_set() and finalized == ['failed']


@pytest.mark.parametrize('data', [
    [{'index': 0, 'embedding': [1.]}, {'index': 0, 'embedding': [2.]}],
    [{'index': 0, 'embedding': [True]}, {'index': 1, 'embedding': [2.]}],
    [{'index': 0, 'embedding': [float('nan')]}, {'index': 1, 'embedding': [2.]}],
    [{'index': 0, 'embedding': []}, {'index': 1, 'embedding': []}],
    [{'index': 0, 'embedding': [1.]}, {'index': 1, 'embedding': [2., 3.]}],
    [{'index': 0, 'embedding': [1.]}],
])
def test_embedding_rejects_ambiguous_or_invalid_vectors(data):
    with pytest.raises(ValueError): embedding_vectors(json.dumps({'data': data}).encode(), 2)


def test_embedding_restores_order_and_validates_requested_dimension():
    content = json.dumps({'data': [{'index': 1, 'embedding': [2.]}, {'index': 0, 'embedding': [1.]}]}).encode()
    assert embedding_vectors(content, 2) == [[1.], [2.]]
    with pytest.raises(ValueError): embedding_vectors(content, 2, 3)


@pytest.mark.parametrize('error', [ResourceClosed(), WorkStopped(), PersistenceOutcomeUnknown(), RuntimeError('bug'), psycopg.OperationalError()])
async def test_memory_optional_retrieval_does_not_swallow_control_or_bugs(monkeypatch, error):
    from agent_service import memory
    async def base(*args): return 'new', ({}, None, 0, [])
    async def failure(*args): raise error
    async def forbidden(*args): pytest.fail('Unexpected snapshot publication')
    monkeypatch.setattr(memory.memory_store, 'snapshot_base', base)
    monkeypatch.setattr(memory, 'embedding', failure)
    monkeypatch.setattr(memory.memory_store, 'snapshot_publish', forbidden)
    with pytest.raises(type(error)): await memory.capture_snapshot(uuid4(), uuid4(), 'task')


async def test_memory_expected_provider_failure_retains_conversation_context(monkeypatch):
    from agent_service import memory
    async def base(*args): return 'new', ({}, None, 0, [])
    async def failure(*args): raise ProviderFailure('EMBEDDING_UNAVAILABLE')
    async def publish(rid, token, snapshot): return snapshot
    monkeypatch.setattr(memory.memory_store, 'snapshot_base', base)
    monkeypatch.setattr(memory, 'embedding', failure)
    monkeypatch.setattr(memory.memory_store, 'snapshot_publish', publish)
    assert (await memory.capture_snapshot(uuid4(), uuid4(), 'task'))['memory_degraded']


async def test_unregistered_structured_provider_cannot_use_runnable_fallback():
    from agent_service.model_adapter import structured
    from agent_service.schemas import FinalDraft
    class Model:
        def with_structured_output(self, schema): pytest.fail('Implicit runnable fallback')
    with pytest.raises(TypeError, match='registered'): await structured(Model(), FinalDraft, [])


async def test_executor_rechecks_authority_after_waiting_for_capacity():
    runtime = resources(); occupied = threading.Event(); release = threading.Event(); denied = asyncio.Event()
    def blocking(): occupied.set(); release.wait(2)
    first = asyncio.create_task(runtime.io.run(blocking, deadline=OperationDeadline.after(3)))
    # Fill all other execution slots without relying on scheduling delays.
    fillers = [asyncio.create_task(runtime.io.run(blocking, deadline=OperationDeadline.after(3)))
               for _ in range(runtime.config.io_workers - 1)]
    await asyncio.get_running_loop().run_in_executor(None, occupied.wait, 1)
    while runtime.io._occupied() < runtime.config.io_workers: await asyncio.sleep(0)
    called = []
    async def check(): denied.set(); raise ExecutionRevoked('Revoked while queued')
    queued = asyncio.create_task(runtime.io.run(lambda: called.append(True),
        deadline=OperationDeadline.after(3), before_submit=check))
    while not runtime.io.queue: await asyncio.sleep(0)
    assert not denied.is_set()
    release.set(); await asyncio.gather(first, *fillers)
    with pytest.raises(ExecutionRevoked): await queued
    assert denied.is_set() and called == []
    assert runtime.registry.occupied == 0


def test_group_failure_selection_prioritizes_control_over_retryable_models():
    control = PersistenceOutcomeUnknown()
    assert select_failure(ExceptionGroup('failures', [ProviderFailure('DOWN'), ExceptionGroup('nested', [control])])) is control
    fatal = ProviderFailure('AUTH', False)
    assert select_failure(ExceptionGroup('failures', [ProviderFailure('DOWN'), fatal])) is fatal


async def test_sdk_stream_timeout_releases_only_after_actual_close():
    runtime = resources(); closed = asyncio.Event(); reading = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b'never'
        async def aclose(self): closed.set()
    async def handler(request): return httpx.Response(200, stream=Stream())
    runtime.http.transports['agent-model'] = httpx.MockTransport(handler)
    limit = OperationDeadline.after(.05)
    with call_scope(limit):
        client = runtime.http.sdk_client('agent-model')
        response = await client.send(client.build_request('GET', 'http://model.test'), stream=True)
    assert runtime.http.snapshot()['agent-model']['occupied'] == 1
    with pytest.raises(TimeoutError): await response.aread()
    assert reading.is_set() and closed.is_set()
    assert runtime.http.snapshot()['agent-model']['occupied'] == 0
    assert not runtime.active


async def test_downstream_budget_is_calculated_after_capacity_and_authorization(monkeypatch):
    runtime = resources(); received = []; entered = asyncio.Event(); release = asyncio.Event()
    async def handler(request):
        received.append(float(request.headers['X-Request-Budget-Seconds']))
        return httpx.Response(200, text='ok')
    runtime.http.transports['rag'] = httpx.MockTransport(handler)
    import execution.scope as scope
    async def authorize(): entered.set(); await release.wait()
    monkeypatch.setattr(scope, 'authorize', authorize)
    limit = OperationDeadline.after(2)
    task = asyncio.create_task(runtime.http.request('rag', 'GET', 'http://rag.test',
        deadline=limit, request_budget=True, headers={'X-Request-Budget-Seconds': '120'}))
    await entered.wait()
    remaining = limit.remaining(); release.set(); await task
    assert 0 < received[0] <= remaining < 2


@pytest.mark.parametrize('mode', ['react', 'plan_execute'])
async def test_partial_publication_unknown_never_regenerates_or_becomes_quota(monkeypatch, mode):
    from agent_service import execution as graph, runner
    from agent_service.runtime import claim_run, BudgetExhausted
    from tests.test_agent_runtime import ScriptedModel
    from langchain_core.messages import AIMessage
    rid = insert_run()
    async with transaction() as conn:
        await conn.execute('UPDATE agent.agent_runs SET mode=%s WHERE id=%s', (mode, rid))
    job = await claim_run(); finals = []
    monkeypatch.setattr(graph, 'model', lambda: ScriptedModel(responses=[AIMessage(content='summary')]))
    if mode == 'react':
        class Agent:
            async def aget_state(self, config):
                from types import SimpleNamespace
                return SimpleNamespace(values={})
            async def ainvoke(self, *args, **kwargs): raise BudgetExhausted()
        monkeypatch.setattr(graph, 'react_agent', lambda saver: Agent())
    else:
        async def partial(*args,**kwargs): return [], {}, [], True
        monkeypatch.setattr(graph, 'run_plan', partial)
    async def final(*args):
        finals.append(args)
        from agent_service.acceptance import render_verified
        from tests.quality_stubs import quality_value
        return render_verified('test task',quality_value('GoalContract',{'original_task':'test task'}),{}, {'answer':'partial','claims':[],'notices':[]},None,prior_partial=True,unavailable='BUDGET')
    async def unknown(*args): raise PersistenceOutcomeUnknown()
    monkeypatch.setattr(graph, 'final_generate', final)
    monkeypatch.setattr(runner, 'publish_result', unknown)
    with pytest.raises(PersistenceOutcomeUnknown): await graph.execute_run(rid, job['lease_token'])
    assert len(finals) == 1
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status,error_code FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
    assert row == {'status': 'running', 'error_code': None}


async def test_failed_graph_fanout_cancels_and_awaits_its_sibling(monkeypatch):
    from agent_service import execution as graph, runner
    from agent_service.runtime import claim_run
    from agent_service.tooling import ToolCallFailed
    from execution.scope import track_call_task, current_attempt
    rid = insert_run(); job = await claim_run()
    running = asyncio.Event(); cancelled = asyncio.Event(); children = []
    async def child():
        track_call_task(); running.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    async def graph_execution(*args):
        children.append(asyncio.create_task(child()))
        await running.wait()
        raise ToolCallFailed()
    monkeypatch.setattr(runner, 'execute_graph', graph_execution)
    await graph.execute_run(rid, job['lease_token'])
    assert cancelled.is_set() and children[0].done()


async def test_startup_manifest_is_prepared_outside_claim_transaction(monkeypatch):
    from agent_service import manifest, startup
    from agent_service.storage import runtime as storage
    runtime = resources(); called = []; original = manifest.execution_manifest
    def inspect():
        assert not any('db-active' in stages for stages in list(runtime.active.values()))
        called.append(True)
        return original()
    monkeypatch.setattr(storage, 'execution_manifest', inspect)
    rid = insert_run(); job = await storage.claim_run()
    assert job['run_id'] == rid and called == [True]


async def test_resume_rejects_changed_provider_configuration(monkeypatch):
    from agent_service import startup
    from agent_service.runtime import claim_run, attempt_exited
    rid = insert_run(); first = await claim_run()
    # No external calls: receipt checks alone permit resumption.
    await attempt_exited(rid, first['lease_token'])
    monkeypatch.setenv('AGENT_MODEL', 'different-model')
    assert await claim_run() is None
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status,error_code,termination_reason FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
    assert row == {'status': 'failed', 'error_code': 'INTERRUPTED_UNKNOWN', 'termination_reason': 'configuration_mismatch'}


async def test_native_structured_model_keeps_usage_and_settles_it(monkeypatch):
    from agent_service import model_adapter, model_calls
    from agent_service.schemas import FinalDraft
    from agent_service.tooling import RunExecutionState
    from agent_service.runtime import claim_run
    rid = insert_run(); job = await claim_run(); runtime = resources()
    async def handler(request):
        assert request.headers.get('authorization')
        return httpx.Response(200, json={'id': 'usage', 'object': 'chat.completion', 'created': 1,
            'model': 'mock', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"answer":"ok"}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}})
    runtime.http.transports['agent-model'] = httpx.MockTransport(handler)
    result = await model_calls.invoke_structured(model_adapter.model(), FinalDraft, [('user', 'task')],
        RunExecutionState(rid, job['lease_token']), 'final', final=True)
    assert result.answer == 'ok'
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status,input_tokens,output_tokens FROM agent.model_calls WHERE run_id=%s', (rid,))).fetchone()
    assert row == {'status': 'succeeded', 'input_tokens': 11, 'output_tokens': 7}


async def test_managed_blocking_tool_runs_on_owned_executor_and_keeps_runtime_injection(monkeypatch):
    from pydantic import BaseModel
    from langchain.tools import ToolRuntime
    from agent_service.tooling import managed_io_tool
    from execution.contracts import ExecutionContext, ToolOutcome
    from datetime import datetime, timedelta, timezone
    class Arguments(BaseModel):
        value: str
    seen = []
    def blocking(context, arguments):
        seen.append((threading.current_thread().name, context.call_id))
        content = arguments['value']
        return content, ToolOutcome(content).to_artifact()
    tool = managed_io_tool(blocking, name='blocking', description='Read blocking SDK', args_schema=Arguments)
    identity = call()
    context = ExecutionContext(identity.task, identity.token, datetime.now(timezone.utc) + timedelta(seconds=10), call_id=identity.id)
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                          tool_call_id='external', store=None)
    result = await tool.ainvoke({'name': 'blocking', 'args': {'value': 'ok', 'runtime': runtime},
                                'id': 'external', 'type': 'tool_call'})
    assert result.content == 'ok' and seen[0][0].startswith('runtime-io') and seen[0][1] == identity.id
    assert 'runtime' not in tool.tool_call_schema.model_fields


async def test_document_embedding_consumers_are_bounded_and_restore_chunk_order(monkeypatch):
    from rag_service import processing
    from tests.native_async import runtimes
    from execution.persistence import using
    runtime = runtimes.get()['rag']; entered = asyncio.Event(); release = asyncio.Event()
    drafts = [{'embedding_input': str(i)} for i in range(100)]
    revision = {'model': 'mock', 'dimensions': 1}; captured = []; active = 0; peak = 0
    async def inputs(job): return {'file_path': 'unused', 'media_type': 'txt', 'title': 'fixture'}, [revision]
    async def prepare(*args, **kwargs): return drafts, []
    async def embed(texts, *args):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        try:
            if active == 4: entered.set()
            await release.wait()
            return [[float(text)] for text in texts]
        finally: active -= 1
    async def publish(job, row, output, warnings, prepared): captured.extend(prepared[0][1])
    monkeypatch.setattr(processing.jobs, 'inputs', inputs)
    monkeypatch.setattr(runtime.cpu, 'run', prepare)
    monkeypatch.setattr(processing, 'embed', embed)
    monkeypatch.setattr(processing.jobs, 'publish', publish)
    with using(runtime):
        task = asyncio.create_task(processing.process({}))
        await entered.wait(); assert not task.done(); release.set(); await task
    assert peak == 4 and captured == [[float(i)] for i in range(100)]


async def test_control_wrapped_by_sdk_retains_original_unknown_signal():
    control = PersistenceOutcomeUnknown('unknown commit')
    wrapped = RuntimeError('SDK connection error'); wrapped.__cause__ = control
    assert select_failure(wrapped) is control


async def test_sdk_wrapped_authorization_unknown_cannot_be_settled_as_model_failure(monkeypatch):
    from agent_service import model_adapter, model_calls
    from agent_service.schemas import FinalDraft
    from agent_service.tooling import RunExecutionState
    from agent_service.runtime import claim_run
    import execution.scope as scope
    rid = insert_run(); job = await claim_run(); sent = []
    async def handler(request): sent.append(True); pytest.fail('Uncertain authorization must not send')
    resources().http.transports['agent-model'] = httpx.MockTransport(handler)
    async def unknown(): raise PersistenceOutcomeUnknown('Authorization commit acknowledgement missing')
    monkeypatch.setattr(scope, 'authorize', unknown)
    with pytest.raises(PersistenceOutcomeUnknown):
        await model_calls.invoke_structured(model_adapter.model(), FinalDraft, [('user', 'task')],
            RunExecutionState(rid, job['lease_token']), 'final', final=True)
    assert not sent
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status FROM agent.model_calls WHERE run_id=%s', (rid,))).fetchone()
    assert row['status'] == 'started'


async def test_experiment_settings_and_snapshot_freezing_use_managed_io(monkeypatch):
    from agent_service.tools import experiments
    from agent_service import tooling
    from execution import inputs
    from types import SimpleNamespace
    seen = []
    def settings(): seen.append(threading.current_thread().name); return 'settings'
    async def snapshot(*args): return {'large': [1, 2, 3]}
    monkeypatch.setattr(tooling, 'load_providers', lambda: [('fixture', SimpleNamespace(name='experiment_analysis'))])
    monkeypatch.setattr(experiments, 'settings_from_env', settings)
    monkeypatch.setattr(inputs, 'experiment_snapshot', snapshot)
    result = await inputs.prepare_inputs(TaskRef(uuid4(), TaskKind.AGENT), uuid4())
    assert result['experiment']['large'] == (1, 2, 3)
    assert seen == ['runtime-io_0']


def test_tokenizer_loading_is_local_and_never_fetches_metadata(monkeypatch):
    from rag_service import chunking
    from transformers import AutoTokenizer
    seen = []
    def local(*args, **kwargs): seen.append(kwargs); return 'cached'
    monkeypatch.setattr(AutoTokenizer, 'from_pretrained', local)
    chunking.tokenizer.cache_clear()
    try:
        assert chunking.tokenizer() == 'cached'
        assert seen[0]['local_files_only'] is True
    finally: chunking.tokenizer.cache_clear()


def test_pdf_cpu_parser_uses_explicit_local_artifacts_preserving_ocr(monkeypatch, tmp_path):
    import docling.document_converter as converters
    from docling.datamodel.settings import settings
    from docling.datamodel.base_models import InputFormat
    from rag_service.parse import parse_docling
    from types import SimpleNamespace
    captured = []
    class Document:
        def iterate_items(self):
            yield SimpleNamespace(label='text', text='fixture', prov=[SimpleNamespace(page_no=1)]), 1
    def converter(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(convert=lambda path: SimpleNamespace(document=Document()))
    monkeypatch.setattr(settings, 'artifacts_path', tmp_path)
    monkeypatch.setattr(converters, 'DocumentConverter', converter)
    result, warnings = parse_docling(tmp_path/'fixture.pdf', 'pdf', local_models=True)
    options = captured[0]['format_options'][InputFormat.PDF].pipeline_options
    assert options.artifacts_path == tmp_path and options.do_ocr and options.do_table_structure
    assert result[0].text == 'fixture' and not warnings
