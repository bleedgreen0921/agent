import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from agent_service import execution
from agent_service.app import app
from agent_service.runs import cancel_run, get_run
from agent_service.runtime import (
    BudgetExhausted,
    claim_run,
    publish_result,
    reserve_model,
    reserve_tool,
    revoke_and_finish,
    settle_model,
    settle_tool,
)
from contracts.v1 import Evidence, EvidenceSearchResponse, SourceLocator
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
    monkeypatch.setattr(execution, "RagClient", FakeRagClient)
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
