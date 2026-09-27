import types
from uuid import uuid4

import httpx
import openai
import pytest

from agent_service import execution
from agent_service import manifest as manifest_module
from agent_service import worker
from agent_service.timeouts import CallTimeouts, configured_timeouts, timeouts_for_manifest
from agent_service.tooling import ToolExecutionContext


def test_timeout_configuration_and_manifest_fallback(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGENT_RAG_TIMEOUT_SECONDS", raising=False)
    assert configured_timeouts() == CallTimeouts(120, 120)
    monkeypatch.setenv("AGENT_MODEL_TIMEOUT_SECONDS", "13.5")
    monkeypatch.setenv("AGENT_RAG_TIMEOUT_SECONDS", "17")
    assert configured_timeouts() == CallTimeouts(13.5, 17)
    manifest = manifest_module.execution_manifest()
    assert manifest["limits"]["model_timeout_seconds"] == 13.5
    assert manifest["limits"]["rag_timeout_seconds"] == 17
    monkeypatch.setenv("AGENT_MODEL_TIMEOUT_SECONDS", "29")
    monkeypatch.setenv("AGENT_RAG_TIMEOUT_SECONDS", "31")
    assert timeouts_for_manifest(manifest) == CallTimeouts(13.5, 17)
    assert timeouts_for_manifest({"limits": {"model_timeout_seconds": 13.5}}) == CallTimeouts(13.5, 31)
    assert timeouts_for_manifest({}) == CallTimeouts(29, 31)
    with pytest.raises(ValueError, match="manifest model_timeout_seconds"):
        timeouts_for_manifest({"limits": {"model_timeout_seconds": True}})


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "-inf", "not-a-number", ""])
@pytest.mark.parametrize("name", ["AGENT_MODEL_TIMEOUT_SECONDS", "AGENT_RAG_TIMEOUT_SECONDS"])
def test_timeout_configuration_rejects_invalid_values(monkeypatch, name, bad):
    monkeypatch.setenv(name, bad)
    with pytest.raises(ValueError, match=name):
        configured_timeouts()


def test_worker_rejects_invalid_timeout_before_starting(monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://user:secret@localhost/test")
    monkeypatch.setenv("AGENT_MODEL_URL", "http://model.test")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    monkeypatch.setenv("AGENT_MODEL_TIMEOUT_SECONDS", "nan")
    with pytest.raises(ValueError, match="AGENT_MODEL_TIMEOUT_SECONDS"):
        worker.main()


def test_model_factory_uses_run_timeout_and_disables_retries(monkeypatch):
    captured = {}
    monkeypatch.setenv("AGENT_MODEL_URL", "http://model.test")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setattr(execution, "ChatOpenAI", lambda **kwargs: captured.update(kwargs))
    token = execution._run_timeouts.set(CallTimeouts(19, 23))
    try:
        execution.model()
    finally:
        execution._run_timeouts.reset(token)
    assert captured["timeout"] == 19
    assert captured["max_retries"] == 0
    assert captured["base_url"] == "http://model.test/v1"


@pytest.mark.parametrize("exception", [
    lambda: openai.APITimeoutError(request=httpx.Request("POST", "http://model.test/v1/chat/completions")),
    lambda: httpx.ReadTimeout("timeout"),
])
def test_react_model_timeout_is_settled_once(monkeypatch, exception):
    run_id, lease_token, call_id = uuid4(), uuid4(), uuid4()
    context = ToolExecutionContext(run_id, lease_token)
    settled = []
    monkeypatch.setattr(execution, "reserve_model", lambda *_args, **_kwargs: call_id)
    monkeypatch.setattr(execution, "settle_model", lambda *args, **kwargs: settled.append((args, kwargs)) or True)
    request = types.SimpleNamespace(runtime=types.SimpleNamespace(context=context), state={"messages": []})
    with pytest.raises(execution.ModelTimeoutError):
        execution.budgeted_model.wrap_model_call(request, lambda _request: (_ for _ in ()).throw(exception()))
    assert len(settled) == 1
    assert settled[0][0][3] == "failed"
    assert settled[0][1]["error_code"] == "MODEL_TIMEOUT"


def test_structured_model_timeout_is_settled_once(monkeypatch):
    run_id, lease_token, call_id = uuid4(), uuid4(), uuid4()
    context = ToolExecutionContext(run_id, lease_token)
    settled = []
    monkeypatch.setattr(execution, "reserve_model", lambda *_args, **_kwargs: call_id)
    monkeypatch.setattr(execution, "settle_model", lambda *args, **kwargs: settled.append((args, kwargs)) or True)
    llm = types.SimpleNamespace(with_structured_output=lambda _schema: types.SimpleNamespace(invoke=lambda _messages: (_ for _ in ()).throw(httpx.ReadTimeout("timeout"))))
    with pytest.raises(execution.ModelTimeoutError):
        execution.invoke_structured(llm, execution.Plan, [], context, "planner")
    assert len(settled) == 1
    assert settled[0][1]["error_code"] == "MODEL_TIMEOUT"
