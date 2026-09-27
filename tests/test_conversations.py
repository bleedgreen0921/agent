import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.postgres import PostgresSaver

from agent_service.app import app as agent_app
from agent_service import execution, memory
from agent_service.checkpoints import with_agent_search_path
from db.connection import connect
from identity.security import new_key
from rag_service.app import app as rag_app

pytestmark = pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")


def team_key():
    team_id = uuid4()
    key_id, raw, digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, str(team_id)))
        conn.execute("INSERT INTO identity.credentials(key_id,kind,team_id,digest) VALUES (%s,'team',%s,%s)", (key_id, team_id, digest))
    return team_id, raw


def user_key(rag, team_raw, name):
    headers = {"Authorization": "Bearer " + team_raw}
    user = rag.post("/v1/team/users", headers=headers, json={"name": name})
    assert user.status_code == 201, user.text
    user_id = user.json()["user_id"]
    key = rag.post(f"/v1/team/users/{user_id}/keys", headers=headers, json={})
    assert key.status_code == 201, key.text
    return UUID(user_id), key.json()["key"], key.json()["key_id"]


def submit(agent, conversation_id, key, text, idem, mode="react"):
    return agent.post(f"/v1/conversations/{conversation_id}/turns",
                      headers={"Authorization": "Bearer " + key, "Idempotency-Key": idem},
                      json={"task": text, "mode": mode})


def complete(run_id, answer="Done"):
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET status='completed',finished_at=now() WHERE id=%s", (run_id,))
        conn.execute("INSERT INTO agent.run_results(id,run_id,answer) VALUES (%s,%s,%s)", (uuid4(), run_id, answer))


def test_user_scope_idempotency_busy_and_cancel():
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        user_a, key_a, key_id = user_key(rag, team, "a")
        _, key_b, _ = user_key(rag, team, "b")
        auth_a = {"Authorization": "Bearer " + key_a}
        auth_b = {"Authorization": "Bearer " + key_b}
        created = agent.post("/v1/conversations", headers=auth_a)
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]
        assert agent.get(f"/v1/conversations/{conversation_id}", headers=auth_b).status_code == 404
        assert agent.get("/v1/conversations", headers=auth_b).json()["items"] == []
        assert agent.get("/v1/memories", headers=auth_b).json()["items"] == []
        assert agent.post("/v1/conversations", headers={"Authorization": "Bearer " + team}).status_code == 401
        assert submit(agent, conversation_id, key_a, "A uses MySQL", "").status_code == 422
        first = submit(agent, conversation_id, key_a, "A uses MySQL", "one")
        assert first.status_code == 202, first.text
        replay = submit(agent, conversation_id, key_a, "A uses MySQL", "one")
        assert replay.status_code == 200 and replay.json()["run_id"] == first.json()["run_id"]
        assert submit(agent, conversation_id, key_a, "different", "one").status_code == 409
        assert submit(agent, conversation_id, key_a, "A uses PostgreSQL", "two").json()["error"]["code"] == "CONVERSATION_BUSY"
        turn_id = first.json()["turn_id"]
        assert agent.get(f"/v1/conversations/{conversation_id}/turns/{turn_id}", headers=auth_b).status_code == 404
        assert agent.post(f"/v1/conversations/{conversation_id}/turns/{turn_id}/cancel", headers=auth_b).status_code == 404
        assert agent.post(f"/v1/conversations/{conversation_id}/turns/{turn_id}/cancel", headers=auth_a).status_code == 200
        second = submit(agent, conversation_id, key_a, "A uses PostgreSQL", "two", "plan_execute")
        assert second.status_code == 202 and second.json()["ordinal"] == 2
        history = agent.get(f"/v1/conversations/{conversation_id}", headers=auth_a).json()["turns"]
        assert history[0]["status"] == "cancelled" and history[0]["answer"] is None
        assert history[1]["user_text"] == "A uses PostgreSQL"
        with connect("AGENT_DATABASE_URL") as conn:
            jobs = conn.execute("SELECT count(*) AS n FROM agent.memory_jobs WHERE kind='extract' AND turn_id IN (%s,%s)", (turn_id, second.json()["turn_id"])).fetchone()["n"]
        assert jobs == 2
        revoked = rag.post(f"/v1/team/users/{user_a}/keys/{key_id}/revoke", headers={"Authorization": "Bearer " + team})
        assert revoked.status_code == 200
        assert agent.get("/v1/memories", headers=auth_a).status_code == 401


def test_context_snapshot_is_frozen_and_degrades(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "snapshot")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        complete(UUID(first.json()["run_id"]), "Acknowledged")
        second = submit(agent, cid, key, "What database does this project use?", "two")
        run_id = UUID(second.json()["run_id"])
        monkeypatch.setattr(memory, "embedding", lambda _: (_ for _ in ()).throw(RuntimeError("down")))
        snapshot = memory.capture_snapshot(run_id, "What database does this project use?")
        assert snapshot["memory_degraded"] is True
        assert snapshot["history"][0]["user"] == "Project A uses MySQL"
        assert snapshot["history"][0]["assistant"] == "Acknowledged"
        monkeypatch.setattr(memory, "embedding", lambda _: ("synthetic", [[1.0, 0.0]]))
        assert memory.capture_snapshot(run_id, "new query") == snapshot
        with connect("AGENT_DATABASE_URL") as conn:
            assert conn.execute("SELECT count(*) AS n FROM agent.run_memory_snapshots WHERE run_id=%s", (run_id,)).fetchone()["n"] == 1
        assert agent.get(f"/v1/conversations/{cid}", headers=auth).status_code == 200


def test_snapshot_keeps_oversized_immediately_previous_turn(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "long-history")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        long_text = "opening detail " + "x" * 9000 + " closing detail"
        first = submit(agent, cid, key, long_text, "one")
        assert first.status_code == 202, first.text
        complete(UUID(first.json()["run_id"]), "Acknowledged")
        second = submit(agent, cid, key, "Continue from the prior turn", "two")
        assert second.status_code == 202, second.text
        monkeypatch.setattr(memory, "embedding", lambda _: (_ for _ in ()).throw(RuntimeError("down")))

        snapshot = memory.capture_snapshot(UUID(second.json()["run_id"]), "Continue from the prior turn")
        assert len(snapshot["history"]) == 1
        assert snapshot["history"][0]["user"].startswith("opening detail ")
        assert snapshot["history"][0]["user"].endswith(" closing detail")
        assert len(snapshot["history"][0]["user"]) == memory.HISTORY_CHARS


def test_snapshot_limits_unsummarized_history_and_keeps_four_recent_turns(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "bounded-history")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        for ordinal in range(1, 34):
            turn = submit(agent, cid, key, f"turn {ordinal}", str(ordinal))
            assert turn.status_code == 202, turn.text
            complete(UUID(turn.json()["run_id"]))
        monkeypatch.setattr(memory, "embedding", lambda _: (_ for _ in ()).throw(RuntimeError("down")))

        unsummarized = submit(agent, cid, key, "snapshot without summary", "34")
        first_snapshot = memory.capture_snapshot(UUID(unsummarized.json()["run_id"]), "snapshot without summary")
        assert [turn["ordinal"] for turn in first_snapshot["history"]] == list(range(2, 34))

        complete(UUID(unsummarized.json()["run_id"]))
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("""INSERT INTO agent.conversation_summaries(id,conversation_id,version,through_ordinal,content)
                VALUES (%s,%s,1,31,'summary through turn 31')""", (uuid4(), UUID(cid)))
        summarized = submit(agent, cid, key, "snapshot with summary", "35")
        second_snapshot = memory.capture_snapshot(UUID(summarized.json()["run_id"]), "snapshot with summary")
        assert second_snapshot["summary_through_ordinal"] == 31
        assert [turn["ordinal"] for turn in second_snapshot["history"]] == [31, 32, 33, 34]


def test_fact_extraction_requires_user_spans_and_is_idempotent(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "facts")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        complete(UUID(first.json()["run_id"]))
        second = submit(agent, cid, key, "这个项目从 MySQL 改用 PostgreSQL", "two")
        first_turn, second_turn = UUID(first.json()["turn_id"]), UUID(second.json()["turn_id"])

        class FakeStructured:
            def invoke(self, _):
                return memory.FactList(facts=[
                    memory.FactDraft(statement="Project A changed from MySQL to PostgreSQL", source_quote="从 MySQL 改用 PostgreSQL", subject="Project A", subject_quote="Project A", subject_turn_id=first_turn),
                    memory.FactDraft(statement="Project B is current", source_quote="nonexistent", subject="Project B", subject_quote="Project B", subject_turn_id=first_turn),
                ])

        class FakeModel:
            def with_structured_output(self, _):
                return FakeStructured()

        monkeypatch.setattr(memory, "model", lambda: FakeModel())
        monkeypatch.setattr(memory, "embedding", lambda _: ("synthetic", [[1.0, 0.0]]))
        job = {"id": uuid4(), "kind": "extract", "turn_id": second_turn, "attempts": 0, "lease_token": uuid4()}
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("UPDATE agent.memory_jobs SET status='running',lease_token=%s WHERE turn_id=%s AND kind='extract'", (job["lease_token"], second_turn))
            job["id"] = conn.execute("SELECT id FROM agent.memory_jobs WHERE turn_id=%s AND kind='extract'", (second_turn,)).fetchone()["id"]
        memory.extract(job)
        memory.extract(job)
        facts = agent.get("/v1/memories", headers=auth).json()["items"]
        assert len(facts) == 1
        assert facts[0]["subject_turn_id"] == str(first_turn)
        assert facts[0]["source_turn_id"] == str(second_turn)
        assert facts[0]["source_quote"] == "从 MySQL 改用 PostgreSQL"


def test_ambiguous_demonstrative_and_inferred_change_are_omitted(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "ambiguous")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A and Project B are active", "one")
        complete(UUID(first.json()["run_id"]))
        second = submit(agent, cid, key, "this project uses Redis", "two")
        first_turn, second_turn = UUID(first.json()["turn_id"]), UUID(second.json()["turn_id"])

        class FakeStructured:
            def invoke(self, _):
                return memory.FactList(facts=[
                    memory.FactDraft(statement="Project A uses Redis", source_quote="this project uses Redis", subject="Project A", subject_quote="Project A", subject_turn_id=first_turn),
                    memory.FactDraft(statement="Project A changed from MySQL to Redis", source_quote="this project uses Redis", subject="Project A", subject_quote="Project A", subject_turn_id=first_turn),
                ])

        class FakeModel:
            def with_structured_output(self, _):
                return FakeStructured()

        monkeypatch.setattr(memory, "model", lambda: FakeModel())
        job = {"turn_id": second_turn, "lease_token": uuid4()}
        with connect("AGENT_DATABASE_URL") as conn:
            job["id"] = conn.execute("SELECT id FROM agent.memory_jobs WHERE turn_id=%s AND kind='extract'", (second_turn,)).fetchone()["id"]
            conn.execute("UPDATE agent.memory_jobs SET status='running',lease_token=%s WHERE id=%s", (job["lease_token"], job["id"]))
        memory.extract(job)
        assert agent.get("/v1/memories", headers=auth).json()["items"] == []


def test_new_value_does_not_create_unspoken_migration(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "change")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        complete(UUID(first.json()["run_id"]))
        second = submit(agent, cid, key, "Project A uses PostgreSQL", "two")
        second_turn = UUID(second.json()["turn_id"])

        class FakeStructured:
            def invoke(self, _):
                return memory.FactList(facts=[
                    memory.FactDraft(statement="Project A uses PostgreSQL", source_quote="Project A uses PostgreSQL", subject="Project A", subject_quote="Project A", subject_turn_id=second_turn),
                    memory.FactDraft(statement="Project A changed from MySQL to PostgreSQL", source_quote="Project A uses PostgreSQL", subject="Project A", subject_quote="Project A", subject_turn_id=second_turn),
                ])

        class FakeModel:
            def with_structured_output(self, _):
                return FakeStructured()

        monkeypatch.setattr(memory, "model", lambda: FakeModel())
        monkeypatch.setattr(memory, "embedding", lambda _: ("synthetic", [[1.0, 0.0]]))
        job = {"turn_id": second_turn, "lease_token": uuid4()}
        with connect("AGENT_DATABASE_URL") as conn:
            job["id"] = conn.execute("SELECT id FROM agent.memory_jobs WHERE turn_id=%s AND kind='extract'", (second_turn,)).fetchone()["id"]
            conn.execute("UPDATE agent.memory_jobs SET status='running',lease_token=%s WHERE id=%s", (job["lease_token"], job["id"]))
        memory.extract(job)
        assert [f["statement"] for f in agent.get("/v1/memories", headers=auth).json()["items"]] == ["Project A uses PostgreSQL"]


def test_retrieval_adds_related_history_without_discarding_old_statement(monkeypatch):
    team_id, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        user_id, key, _ = user_key(rag, team, "history")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        complete(UUID(first.json()["run_id"]))
        second = submit(agent, cid, key, "Project A uses PostgreSQL", "two")
        complete(UUID(second.json()["run_id"]))
        third = submit(agent, cid, key, "What has Project A used?", "three")
        first_turn, second_turn = UUID(first.json()["turn_id"]), UUID(second.json()["turn_id"])
        with connect("AGENT_DATABASE_URL") as conn:
            for turn, statement, quote, vector in (
                (first_turn, "Project A uses MySQL", "Project A uses MySQL", "[0,1]"),
                (second_turn, "Project A uses PostgreSQL", "Project A uses PostgreSQL", "[1,0]"),
            ):
                conn.execute("""INSERT INTO agent.personal_facts(id,team_id,user_id,source_turn_id,statement,
                    source_quote,subject,subject_quote,subject_turn_id,stated_at,embedding,embedding_model)
                    VALUES (%s,%s,%s,%s,%s,%s,'Project A','Project A',%s,now(),%s::vector,'synthetic')""",
                    (uuid4(), team_id, user_id, turn, statement, quote, turn, vector))
            for i in range(6):
                conn.execute("""INSERT INTO agent.personal_facts(id,team_id,user_id,source_turn_id,statement,
                    source_quote,subject,subject_quote,subject_turn_id,stated_at,embedding,embedding_model)
                    VALUES (%s,%s,%s,%s,%s,%s,'Other','Other',%s,now(),%s::vector,'synthetic')""",
                    (uuid4(), team_id, user_id, first_turn, f"Other fact {i}", "Other", first_turn, "[0.9,0.1]"))
        monkeypatch.setattr(memory, "embedding", lambda _: ("synthetic", [[1.0, 0.0]]))
        snapshot = memory.capture_snapshot(UUID(third.json()["run_id"]), "What has Project A used?")
        a_facts = [f["statement"] for f in snapshot["facts"] if f["subject"] == "Project A"]
        assert set(a_facts) == {"Project A uses MySQL", "Project A uses PostgreSQL"}
        assert snapshot["memory_degraded"] is False


def test_background_extraction_retries_are_bounded(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "retry")
        cid = agent.post("/v1/conversations", headers={"Authorization": "Bearer " + key}).json()["conversation_id"]
        turn = submit(agent, cid, key, "Project A uses MySQL", "one")
        turn_id = UUID(turn.json()["turn_id"])
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("UPDATE agent.memory_jobs SET created_at='2000-01-01' WHERE kind='extract' AND turn_id=%s", (turn_id,))
        monkeypatch.setattr(memory, "extract", lambda _: (_ for _ in ()).throw(RuntimeError("model down")))
        for _ in range(3):
            assert memory.process_next_job() is True
        with connect("AGENT_DATABASE_URL") as conn:
            row = conn.execute("SELECT status,attempts,error_code FROM agent.memory_jobs WHERE kind='extract' AND turn_id=%s", (turn_id,)).fetchone()
        assert row == {"status": "failed", "attempts": 3, "error_code": "RuntimeError"}


def test_summary_version_is_async_and_does_not_charge_run_budget(monkeypatch):
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "summary")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        run_id, turn_id = UUID(first.json()["run_id"]), UUID(first.json()["turn_id"])
        complete(run_id, "Acknowledged")
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("INSERT INTO agent.memory_jobs(id,kind,turn_id) VALUES (%s,'summary',%s)", (uuid4(), turn_id))
            before = conn.execute("SELECT model_calls_used FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()["model_calls_used"]

        class FakeStructured:
            def invoke(self, _):
                return memory.SummaryDraft(summary="Project A was described as using MySQL.")

        class FakeModel:
            def with_structured_output(self, _):
                return FakeStructured()

        monkeypatch.setattr(memory, "model", lambda: FakeModel())
        memory.summarize({"turn_id": turn_id})
        with connect("AGENT_DATABASE_URL") as conn:
            row = conn.execute("SELECT version,through_ordinal,content FROM agent.conversation_summaries WHERE conversation_id=%s", (UUID(cid),)).fetchone()
            after = conn.execute("SELECT model_calls_used FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()["model_calls_used"]
        assert row == {"version": 1, "through_ordinal": 1, "content": "Project A was described as using MySQL."}
        assert before == after
        second = submit(agent, cid, key, "What does the project use?", "two")
        monkeypatch.setattr(memory, "embedding", lambda _: (_ for _ in ()).throw(RuntimeError("down")))
        snapshot = memory.capture_snapshot(UUID(second.json()["run_id"]), "question")
        assert snapshot["summary_version"] == 1
        assert snapshot["summary_through_ordinal"] == 1


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
def test_both_modes_receive_prior_turn_and_memory_notice(monkeypatch, mode):
    class Scripted(FakeMessagesListChatModel):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

        def with_structured_output(self, schema, **kwargs):
            def value(_):
                if schema is execution.Plan:
                    return execution.Plan(steps=[execution.PlanStep(goal="Answer", completion_condition="Answered")])
                return execution.FinalDraft(answer="Answered", claims=[])
            return RunnableLambda(value)

    monkeypatch.setenv("AGENT_MODEL", "scripted")
    monkeypatch.setattr(execution, "model", lambda: Scripted(responses=[AIMessage(content="Step summary")]))
    monkeypatch.setattr(memory, "embedding", lambda _: (_ for _ in ()).throw(RuntimeError("down")))
    _, team = team_key()
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        _, key, _ = user_key(rag, team, "dual-mode")
        auth = {"Authorization": "Bearer " + key}
        cid = agent.post("/v1/conversations", headers=auth).json()["conversation_id"]
        first = submit(agent, cid, key, "Project A uses MySQL", "one")
        complete(UUID(first.json()["run_id"]), "I recorded that")
        second = submit(agent, cid, key, "What does the project use?", "two", mode)
        run_id, token = UUID(second.json()["run_id"]), uuid4()
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("""UPDATE agent.agent_runs SET status='running',lease_token=%s,
                leased_until=now()+interval '2 minutes',execution_deadline_at=now()+interval '5 minutes',
                started_at=now() WHERE id=%s""", (token, run_id))
        execution.execute_run(run_id, token)
        result = agent.get(f"/v1/conversations/{cid}/turns/{second.json()['turn_id']}", headers=auth).json()
        assert result["status"] == "completed", result
        assert any(n["code"] == "MEMORY_DEGRADED" for n in result["result"]["notices"])
        with PostgresSaver.from_conn_string(with_agent_search_path(os.environ["AGENT_DATABASE_URL"])) as saver:
            if mode == "react":
                state = execution.react_agent(saver).get_state({"configurable": {"thread_id": str(run_id)}}).values
                text = str(state["messages"])
            else:
                # The plan graph stores the initial contextual task under the Run thread.
                from langgraph.graph import StateGraph, START, END
                from typing import TypedDict
                class State(TypedDict):
                    task: str
                graph = StateGraph(State)
                graph.add_node("noop", lambda state: state)
                graph.add_edge(START, "noop")
                graph.add_edge("noop", END)
                text = str(graph.compile(checkpointer=saver).get_state({"configurable": {"thread_id": str(run_id)}}).values)
        assert "Project A uses MySQL" in text
