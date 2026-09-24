import hashlib
import json
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain.tools import ToolRuntime, tool

from agent_service import execution
from agent_service.rag_client import RagError
from agent_service.tools import rag as rag_tools
from agent_service.tooling import RecoverableToolError, ToolExecutionContext
from agent_service.app import app
from agent_service.runs import cancel_run, get_run
from agent_service.runtime import (
    BudgetExhausted,
    claim_run,
    mark_checkpointed_calls,
    publish_result,
    reserve_model,
    reserve_tool,
    revoke_and_finish,
    safe_to_resume,
    settle_model,
    settle_tool,
)
from contracts.v1 import ErrorCode, Evidence, EvidenceSearchResponse, RunResponse, SourceLocator
from db.connection import connect
from identity.security import new_key


pytestmark = pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")


def credential(kind: str = "team", team_id: UUID | None = None) -> tuple[UUID | None, str]:
    if kind == "team" and team_id is None:
        team_id = uuid4()
        with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
            conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, "agent-" + uuid4().hex))
    key_id, raw, digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.credentials(key_id,kind,team_id,digest) VALUES (%s,%s,%s,%s)", (key_id, kind, team_id, digest))
    return team_id, raw


def insert_run(team_id: UUID, *, status: str = "queued", created_delta: timedelta = timedelta(), queue_delta: timedelta = timedelta(minutes=5), lease_delta: timedelta | None = None) -> UUID:
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute(
            """INSERT INTO agent.agent_runs(
                id,team_id,key_id,task,mode,status,request_digest,queue_deadline_at,created_at,
                started_at,execution_deadline_at,lease_token,leased_until)
                VALUES (%s,%s,'test-key','test task','react',%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                run_id,
                team_id,
                status,
                hashlib.sha256(str(run_id).encode()).digest(),
                now + queue_delta,
                now + created_delta,
                now if status == "running" else None,
                now + timedelta(minutes=5) if status == "running" else None,
                uuid4() if status == "running" else None,
                now + lease_delta if lease_delta is not None else None,
            ),
        )
    return run_id


def quiesce_runs() -> None:
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("""UPDATE agent.agent_runs SET status='cancelled',finished_at=now(),
            termination_reason='test_cleanup',lease_token=NULL,leased_until=NULL
            WHERE status IN ('queued','running','cancelling')""")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        def value(_messages):
            if schema is execution.Plan:
                return execution.Plan(steps=[execution.PlanStep(goal="Answer the task", completion_condition="A concise answer is available")])
            return execution.FinalDraft(
                answer="Synthetic answer",
                claims=[execution.DraftClaim(text="Synthetic answer", support="unverified", evidence_ids=[], reason="No knowledge-base evidence was requested")],
            )

        return RunnableLambda(value)


class EvidenceModel(ScriptedModel):
    def with_structured_output(self, schema, **kwargs):
        if schema is not execution.FinalDraft:
            return super().with_structured_output(schema, **kwargs)
        return RunnableLambda(
            lambda _messages: execution.FinalDraft(
                answer="Retention is seven years.",
                claims=[execution.DraftClaim(text="Seven years", support="evidence", evidence_ids=["ev-tool"])],
            )
        )


class InvalidPlanModel(ScriptedModel):
    def with_structured_output(self, schema, **kwargs):
        if schema is execution.Plan:
            def invalid(_messages):
                raise ValueError("invalid structured plan")

            return RunnableLambda(invalid)
        return super().with_structured_output(schema, **kwargs)


seen_tool_context: list[tuple[UUID | None, UUID]] = []


@tool
def echo_value(value: str, runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Return a value for tool-provider integration testing."""
    seen_tool_context.append((runtime.context.team_id, runtime.context.run_id))
    return "echo:" + value


@tool
def recoverable_value(runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Raise a recoverable provider error for integration testing."""
    raise RecoverableToolError("TEMPORARY_TOOL_ERROR", "The tool is temporarily unavailable")


@tool
def broken_value(runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Raise an unexpected provider error for integration testing."""
    raise RuntimeError("provider implementation failed")


def extra_tools():
    return [echo_value, recoverable_value, broken_value]


def configure_extra_tools(monkeypatch):
    provider = types.ModuleType("test_extra_tool_provider")
    provider.tools = extra_tools
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", provider.__name__ + ":tools")


def checkpoint_tool_messages(run_id: UUID) -> list[ToolMessage]:
    dsn = execution.with_agent_search_path(os.environ["AGENT_DATABASE_URL"])
    with execution.PostgresSaver.from_conn_string(dsn) as saver:
        snapshot = execution.react_agent(saver).get_state({"configurable": {"thread_id": str(run_id)}})
    return [message for message in snapshot.values["messages"] if isinstance(message, ToolMessage)]


class FakeRagClient:
    class Client:
        def close(self):
            pass

    def __init__(self, *_args, **_kwargs):
        self.client = self.Client()

    def search(self, _run_id, _tool_call_id, _request):
        evidence = Evidence(
            evidence_id="ev-tool",
            document_id="doc-tool",
            document_version_id="ver-tool",
            title="Retention policy",
            content="Records are kept for seven years.",
            source_locator=SourceLocator(kind="txt", line_start=3, line_end=3),
            rank=1,
        )
        return EvidenceSearchResponse(request_id="rag-1", retrieval_id="ret-1", status="ok", evidences=[evidence], degradations=[])


class UnauthorizedRagClient(FakeRagClient):
    def search(self, _run_id, _tool_call_id, _request):
        raise RagError(401, ErrorCode.UNAUTHENTICATED)


class UnavailableRagClient(FakeRagClient):
    def search(self, _run_id, _tool_call_id, _request):
        raise RagError(503, ErrorCode.RAG_UNAVAILABLE)


class PartialPlanModel(ScriptedModel):
    final_prompt: dict | None = None

    def __init__(self):
        super().__init__(responses=[AIMessage(content="unused")])

    def with_structured_output(self, schema, **kwargs):
        if schema is execution.Plan:
            return RunnableLambda(
                lambda _messages: execution.Plan(
                    steps=[
                        execution.PlanStep(goal="Collect policy A", completion_condition="Policy A is summarized"),
                        execution.PlanStep(goal="Compare policy B", completion_condition="The policies are compared"),
                    ]
                )
            )

        def final(messages):
            raw = messages[-1].content if hasattr(messages[-1], "content") else messages[-1][1]
            self.final_prompt = json.loads(raw)
            return execution.FinalDraft(
                answer="Partial answer from policy A.",
                claims=[execution.DraftClaim(text="Policy A was collected", support="evidence", evidence_ids=["ev-prior"])],
            )

        return RunnableLambda(final)


class PartialStepAgent:
    def __init__(self):
        self.prompts = []

    def get_state(self, _config):
        return types.SimpleNamespace(values={})

    def invoke(self, payload, *, config, context):
        prompt = json.loads(payload["messages"][0]["content"])
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            context.evidence["ev-prior"] = {
                "evidence_id": "ev-prior",
                "document_id": "doc-prior",
                "document_version_id": "ver-prior",
                "title": "Policy A",
                "content": "Policy A retains records for seven years.",
                "source_locator": {"kind": "txt", "line_start": 1, "line_end": 1},
            }
            context.notices.append({"code": "STEP_ONE_COMPLETE", "message": "Policy A was collected."})
            return {"messages": [AIMessage(content="Policy A summary")]}
        raise BudgetExhausted("tool")


def test_run_api_idempotency_isolation_cancel_and_trace():
    team1, key1 = credential()
    _, key2 = credential()
    _, admin_key = credential("admin")
    headers = {"Authorization": "Bearer " + key1, "Idempotency-Key": "same-request"}
    with TestClient(app) as client:
        first = client.post("/v1/runs", headers=headers, json={"task": "check policy", "mode": "react"})
        assert first.status_code == 202
        replay = client.post("/v1/runs", headers=headers, json={"task": "check policy", "mode": "react"})
        assert replay.status_code == 200
        assert replay.json()["run_id"] == first.json()["run_id"]
        conflict = client.post("/v1/runs", headers=headers, json={"task": "different", "mode": "react"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        run_id = first.json()["run_id"]
        assert client.get(f"/v1/runs/{run_id}", headers={"Authorization": "Bearer " + key2}).status_code == 404
        cancelled = client.post(f"/v1/runs/{run_id}/cancel", headers={"Authorization": "Bearer " + key1})
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        trace = client.get(f"/v1/admin/runs/{run_id}/trace", headers={"Authorization": "Bearer " + admin_key})
        assert trace.status_code == 200
        assert trace.json()["run"]["team_id"] == str(team1)
        assert client.get(f"/v1/admin/runs/{run_id}/trace", headers={"Authorization": "Bearer " + key1}).status_code == 401
        timeline = client.get(f"/v1/admin/runs/{run_id}/timeline", headers={"Authorization": "Bearer " + admin_key})
        assert timeline.status_code == 200
        assert timeline.json()["manifest"] is None
        assert client.get(f"/v1/admin/runs/{run_id}/timeline", headers={"Authorization": "Bearer " + key1}).status_code == 401
        missing = client.get(f"/v1/admin/runs/{uuid4()}/timeline", headers={"Authorization": "Bearer " + admin_key})
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_claim_budgets_atomic_result_and_historical_snapshot(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "1")
    quiesce_runs()
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    assert claimed and claimed["run_id"] == run_id
    token = claimed["lease_token"]

    last_model = None
    for index in range(15):
        last_model = reserve_model(run_id, token, "react", "mock-model", input_summary={"message_count": index + 1})
        assert settle_model(run_id, token, last_model, "succeeded", output_summary={"message_count": 1})
    with pytest.raises(BudgetExhausted):
        reserve_model(run_id, token, "react", "mock-model")
    final_call = reserve_model(run_id, token, "final", "mock-model", final=True)
    assert settle_model(run_id, token, final_call, "succeeded")

    tool_call = reserve_tool(run_id, token, "rag.search", {"query_sha256": "safe"}, triggering_model_call_id=last_model)
    assert settle_tool(run_id, token, tool_call, "succeeded", evidence_ids=["ev-1"])
    evidence = {
        "ev-1": {
            "evidence_id": "ev-1",
            "document_id": "doc-1",
            "document_version_id": "ver-1",
            "title": "Policy",
            "content": "Retention is seven years.",
            "source_locator": {"kind": "txt", "line_start": 4, "line_end": 4},
        }
    }
    draft = {
        "answer": "Retention is seven years.",
        "claims": [{"text": "Seven years", "support": "evidence", "evidence_ids": ["ev-1"], "reason": None}],
        "citations": [evidence["ev-1"]],
        "notices": [],
    }
    assert publish_result(run_id, token, draft, evidence, False, "model_finished")
    result = get_run(run_id, team_id)
    assert result["status"] == "completed"
    assert result["result"]["citations"][0]["content"] == "Retention is seven years."
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT model_calls_used,tool_calls_used FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()
        linked = conn.execute("SELECT triggering_model_call_id,argument_summary FROM agent.tool_calls WHERE id=%s", (tool_call,)).fetchone()
    assert row == {"model_calls_used": 16, "tool_calls_used": 1}
    assert linked["triggering_model_call_id"] == last_model
    assert linked["argument_summary"] == {"query_sha256": "safe"}


def test_queue_timeout_unknown_interruption_and_running_cancel(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "1")
    quiesce_runs()
    team_id, _ = credential()
    expired = insert_run(team_id, queue_delta=timedelta(seconds=-1), created_delta=timedelta(days=-3))
    interrupted = insert_run(team_id, status="running", created_delta=timedelta(days=-2), lease_delta=timedelta(seconds=-1))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status) VALUES (%s,%s,'react','mock','started')", (uuid4(), interrupted))
    assert claim_run() is None
    with connect("AGENT_DATABASE_URL") as conn:
        states = {row["id"]: (row["status"], row["error_code"]) for row in conn.execute("SELECT id,status,error_code FROM agent.agent_runs WHERE id IN (%s,%s)", (expired, interrupted)).fetchall()}
    assert states[expired] == ("failed", "QUEUE_TIMEOUT")
    assert states[interrupted] == ("failed", "INTERRUPTED_UNKNOWN")

    cancellable = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    assert claimed and claimed["run_id"] == cancellable
    payload, status = cancel_run(cancellable, team_id)
    assert status == 202 and payload["status"] == "cancelling"
    revoke_and_finish(cancellable, claimed["lease_token"], "cancel")
    assert get_run(cancellable, team_id)["status"] == "cancelled"


def test_resume_requires_every_settled_call_to_be_checkpointed():
    quiesce_runs()
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    token = claimed["lease_token"]
    call_id = reserve_model(run_id, token, "react", "mock-model")
    assert settle_model(run_id, token, call_id, "succeeded")
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute(
            "INSERT INTO agent.checkpoints(thread_id,checkpoint_ns,checkpoint_id,checkpoint,metadata) VALUES (%s,'','older','{}','{}')",
            (str(run_id),),
        )
        assert not safe_to_resume(conn, run_id)

    assert mark_checkpointed_calls(run_id, token)
    with connect("AGENT_DATABASE_URL") as conn:
        assert safe_to_resume(conn, run_id)

    final_call = reserve_model(run_id, token, "final", "mock-model", final=True)
    assert settle_model(run_id, token, final_call, "succeeded")
    with connect("AGENT_DATABASE_URL") as conn:
        assert not safe_to_resume(conn, run_id)


def test_expired_cancelling_run_is_finalized(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "1")
    quiesce_runs()
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    reserve_model(run_id, claimed["lease_token"], "react", "mock-model")
    payload, status = cancel_run(run_id, team_id)
    assert status == 202 and payload["status"] == "cancelling"
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))

    assert claim_run() is None

    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("SELECT status,termination_reason,lease_token FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()
        call = conn.execute("SELECT status,error_code FROM agent.model_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert run == {"status": "cancelled", "termination_reason": "cancelled", "lease_token": None}
    assert call == {"status": "interrupted_unknown", "error_code": "INTERRUPTED_UNKNOWN"}


@pytest.mark.parametrize(("mode", "expected_calls", "expected_steps"), [("react", 2, 0), ("plan_execute", 3, 1)])
def test_langgraph_dual_mode_execution(monkeypatch, mode, expected_calls, expected_steps):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=[AIMessage(content="Execution summary")]))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET mode=%s WHERE id=%s", (mode, run_id))
    claimed = claim_run()
    assert claimed and claimed["run_id"] == run_id
    execution.execute_run(run_id, claimed["lease_token"])
    result = get_run(run_id, team_id)
    assert result["status"] == "completed"
    assert result["result"]["answer"] == "Synthetic answer"
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("SELECT model_calls_used FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()
        steps = conn.execute("SELECT count(*) AS n FROM agent.run_steps WHERE run_id=%s AND status='completed'", (run_id,)).fetchone()["n"]
    assert run["model_calls_used"] == expected_calls
    assert steps == expected_steps


def test_react_tool_execution_and_citation_snapshot(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "service-token")
    messages = [
        AIMessage(content="", tool_calls=[{"name": "search_evidence", "args": {"query": "retention", "top_k": 5}, "id": "tool-1", "type": "tool_call"}]),
        AIMessage(content="Evidence was found."),
    ]
    monkeypatch.setattr(execution, "model", lambda: EvidenceModel(responses=messages))
    monkeypatch.setattr(rag_tools, "RagClient", FakeRagClient)
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    execution.execute_run(run_id, claimed["lease_token"])
    result = get_run(run_id, team_id)
    assert result["status"] == "completed"
    assert result["result"]["citations"][0]["evidence_id"] == "ev-tool"
    with connect("AGENT_DATABASE_URL") as conn:
        tool_call = conn.execute("SELECT status,retrieval_id,evidence_ids,triggering_model_call_id FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert tool_call["status"] == "succeeded"
    assert tool_call["retrieval_id"] == "ret-1"
    assert tool_call["evidence_ids"] == ["ev-tool"]
    assert tool_call["triggering_model_call_id"] is not None


def test_fatal_tool_auth_error_terminates_run(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "service-token")
    messages = [AIMessage(content="", tool_calls=[{"name": "search_evidence", "args": {"query": "retention"}, "id": "tool-auth", "type": "tool_call"}])]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    monkeypatch.setattr(rag_tools, "RagClient", UnauthorizedRagClient)
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    execution.execute_run(run_id, claimed["lease_token"])
    result = get_run(run_id, team_id)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "AUTH_FAILED"
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call == {"status": "failed", "error_code": "UNAUTHENTICATED"}


def test_rag_unavailable_degrades_without_failing_run(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "service-token")
    messages = [
        AIMessage(content="", tool_calls=[{"name": "search_evidence", "args": {"query": "retention"}, "id": "tool-unavailable", "type": "tool_call"}]),
        AIMessage(content="RAG was unavailable."),
    ]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    monkeypatch.setattr(rag_tools, "RagClient", UnavailableRagClient)
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()

    execution.execute_run(run_id, claimed["lease_token"])

    result = get_run(run_id, team_id)
    assert result["status"] == "completed"
    assert any(item["code"] == "RAG_UNAVAILABLE" for item in result["result"]["notices"])
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call == {"status": "degraded", "error_code": "RAG_UNAVAILABLE"}


def test_invalid_plan_has_stable_failure_code(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setattr(execution, "model", lambda: InvalidPlanModel(responses=[AIMessage(content="unused")]))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET mode='plan_execute' WHERE id=%s", (run_id,))
    claimed = claim_run()
    execution.execute_run(run_id, claimed["lease_token"])
    result = get_run(run_id, team_id)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "INVALID_PLAN"


def test_plan_budget_partial_preserves_prior_step_state(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    plan_model = PartialPlanModel()
    step_agent = PartialStepAgent()
    monkeypatch.setattr(execution, "model", lambda: plan_model)
    monkeypatch.setattr(execution, "react_agent", lambda saver: step_agent)
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET mode='plan_execute' WHERE id=%s", (run_id,))
    claimed = claim_run()

    execution.execute_run(run_id, claimed["lease_token"])

    result = get_run(run_id, team_id)
    assert result["status"] == "partial"
    assert result["result"]["citations"][0]["evidence_id"] == "ev-prior"
    assert any(item["code"] == "STEP_ONE_COMPLETE" for item in result["result"]["notices"])
    assert plan_model.final_prompt["execution_summaries"] == ["Policy A summary"]
    assert step_agent.prompts[1]["prior_step_summaries"] == ["Policy A summary"]
    assert step_agent.prompts[1]["available_evidence"][0]["evidence_id"] == "ev-prior"
    with connect("AGENT_DATABASE_URL") as conn:
        steps = conn.execute("SELECT status,error_code FROM agent.run_steps WHERE run_id=%s ORDER BY ordinal", (run_id,)).fetchall()
    assert steps == [
        {"status": "completed", "error_code": None},
        {"status": "failed", "error_code": "QUOTA_EXCEEDED"},
    ]


def test_global_concurrency_slots_are_atomic(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "2")
    team_id, _ = credential()
    run_ids = {insert_run(team_id, created_delta=timedelta(days=-1, seconds=index)) for index in range(4)}
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = [claim for claim in pool.map(lambda _: claim_run(), range(4)) if claim]
        assert len(claims) == 2
        assert len({claim["run_id"] for claim in claims}) == 2
        assert {claim["run_id"] for claim in claims} <= run_ids
        with connect("AGENT_DATABASE_URL") as conn:
            running = conn.execute("SELECT count(*) AS n FROM agent.agent_runs WHERE id=ANY(%s) AND status='running'", (list(run_ids),)).fetchone()["n"]
        assert running == 2
    finally:
        quiesce_runs()


def test_non_rag_tool_provider_runs_without_execution_changes(monkeypatch):
    quiesce_runs()
    seen_tool_context.clear()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    messages = [
        AIMessage(content="", tool_calls=[{"name": "echo_value", "args": {"value": "hello"}, "id": "extra-1", "type": "tool_call"}]),
        AIMessage(content="The extra tool returned its value."),
    ]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()
    execution.execute_run(run_id, claimed["lease_token"])
    assert get_run(run_id, team_id)["status"] == "completed"
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT tool_name,status,argument_summary FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call["tool_name"] == "echo_value"
    assert call["status"] == "succeeded"
    assert call["argument_summary"]["argument_names"] == ["value"]
    assert "hello" not in json.dumps(call["argument_summary"])
    assert seen_tool_context == [(team_id, run_id)]


@pytest.mark.parametrize(
    "tool_call",
    [
        {"name": "unknown_tool", "args": {}, "id": "unknown-1", "type": "tool_call"},
        {"name": "echo_value", "args": {}, "id": "invalid-1", "type": "tool_call"},
    ],
    ids=["unknown-tool", "invalid-arguments"],
)
def test_langgraph_rejected_tool_call_is_traced_as_failed(monkeypatch, tool_call):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    messages = [AIMessage(content="", tool_calls=[tool_call]), AIMessage(content="The tool request was rejected.")]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()

    execution.execute_run(run_id, claimed["lease_token"])

    assert get_run(run_id, team_id)["status"] == "completed"
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call == {"status": "failed", "error_code": "TOOL_CALL_REJECTED"}
    tool_messages = checkpoint_tool_messages(run_id)
    assert any(message.tool_call_id == tool_call["id"] and message.status == "error" for message in tool_messages)


def test_recoverable_tool_error_has_consistent_message_and_trace_status(monkeypatch):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    messages = [
        AIMessage(content="", tool_calls=[{"name": "recoverable_value", "args": {}, "id": "recoverable-1", "type": "tool_call"}]),
        AIMessage(content="The temporary tool error was handled."),
    ]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()

    execution.execute_run(run_id, claimed["lease_token"])

    assert get_run(run_id, team_id)["status"] == "completed"
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call == {"status": "failed", "error_code": "TEMPORARY_TOOL_ERROR"}
    tool_messages = checkpoint_tool_messages(run_id)
    assert any(message.tool_call_id == "recoverable-1" and message.status == "error" for message in tool_messages)


def test_unhandled_tool_error_has_distinct_public_run_failure(monkeypatch):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL", "scripted")
    messages = [AIMessage(content="", tool_calls=[{"name": "broken_value", "args": {}, "id": "broken-1", "type": "tool_call"}])]
    monkeypatch.setattr(execution, "model", lambda: ScriptedModel(responses=messages))
    team_id, _ = credential()
    run_id = insert_run(team_id, created_delta=timedelta(days=-1))
    claimed = claim_run()

    execution.execute_run(run_id, claimed["lease_token"])

    result = RunResponse.model_validate(get_run(run_id, team_id))
    assert result.status == "failed"
    assert result.error.code == ErrorCode.TOOL_CALL_FAILED
    assert result.error.message == "A tool provider failed"
    with connect("AGENT_DATABASE_URL") as conn:
        call = conn.execute("SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s", (run_id,)).fetchone()
    assert call == {"status": "failed", "error_code": "TOOL_CALL_FAILED"}
