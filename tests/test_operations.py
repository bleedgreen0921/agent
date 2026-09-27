import hashlib
import os
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_service.app import app as agent_app
from agent_service import operations
from db.connection import connect
from db import readiness
from identity.security import new_key
from rag_service.app import app as rag_app


HAS_DATABASE = all(os.environ.get(name) for name in (
    "MIGRATION_DATABASE_URL", "AGENT_DATABASE_URL", "RAG_DATABASE_URL", "IDENTITY_ADMIN_DATABASE_URL"
))


def _identity():
    teams = [uuid4(), uuid4()]
    credentials = [new_key(), new_key(), new_key()]
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        for team in teams:
            conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team, "operations-" + uuid4().hex))
        for index, (key_id, _, digest) in enumerate(credentials):
            kind = "admin" if index == 2 else "team"
            team = None if index == 2 else teams[index]
            conn.execute("INSERT INTO identity.credentials(key_id,kind,team_id,digest) VALUES (%s,%s,%s,%s)",
                         (key_id, kind, team, digest))
    return teams, [{"Authorization": "Bearer " + item[1]} for item in credentials]


def _run(team_id, created_at, *, status="queued", queue_deadline=None, leased_until=None,
         execution_deadline=None, task="secret task", run_id=None, conn=None):
    run_id = run_id or uuid4()
    queue_deadline = queue_deadline or created_at + timedelta(days=2)
    with nullcontext(conn) if conn is not None else connect("AGENT_DATABASE_URL") as db:
        db.execute("""INSERT INTO agent.agent_runs(
            id,team_id,key_id,task,mode,status,request_digest,created_at,queue_deadline_at,
            leased_until,execution_deadline_at)
            VALUES (%s,%s,'operations-key',%s,'react',%s,%s,%s,%s,%s,%s)""",
            (run_id, team_id, task, status, hashlib.sha256(run_id.bytes).digest(), created_at,
             queue_deadline, leased_until, execution_deadline))
    return run_id


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_summary_excludes_running_runs_unsafe_to_resume(monkeypatch):
    # Keep this case independent of claimable rows left by other database tests.
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET status='failed' WHERE status IN ('queued','running','cancelling')")

        class SummaryConnection:
            read_only = False

            def execute(self, *args):
                return conn.execute(*args)

        monkeypatch.setattr(operations, "connect", lambda _: nullcontext(SummaryConnection()))
        team_id = uuid4()
        now = datetime.now(timezone.utc)
        # The run creation helper needs a real team for its foreign key.
        with connect("IDENTITY_ADMIN_DATABASE_URL") as identity_conn:
            identity_conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)",
                                  (team_id, "summary-" + uuid4().hex))

        unsafe_ids = []
        for kind, status, checkpointed in (
            ("model", "started", True),
            ("model", "succeeded", False),
            ("tool", "started", True),
            ("tool", "succeeded", False),
        ):
            run_id = _run(team_id, now - timedelta(minutes=20 + len(unsafe_ids)), status="running",
                          leased_until=now - timedelta(minutes=1),
                          execution_deadline=now + timedelta(hours=1), conn=conn)
            unsafe_ids.append(run_id)
            if kind == "model":
                conn.execute("""INSERT INTO agent.model_calls(id,run_id,purpose,model,status,checkpointed)
                    VALUES (%s,%s,'react','mock',%s,%s)""", (uuid4(), run_id, status, checkpointed))
            else:
                conn.execute("""INSERT INTO agent.tool_calls(id,run_id,tool_name,status,checkpointed)
                    VALUES (%s,%s,'search',%s,%s)""", (uuid4(), run_id, status, checkpointed))

        empty = operations.run_queue_summary()
        assert empty == {
            "queued_within_deadline": 0, "queued_past_deadline": 0,
            "running_lease_valid": 0, "running_lease_expired": 4,
            "cancelling": 0, "oldest_claimable_created_at": None,
        }

        safe_at = now - timedelta(minutes=10)
        safe = _run(team_id, safe_at, status="running", leased_until=now - timedelta(minutes=1),
                    execution_deadline=now + timedelta(hours=1), conn=conn)
        conn.execute("""INSERT INTO agent.model_calls(id,run_id,purpose,model,status,checkpointed)
            VALUES (%s,%s,'react','mock','succeeded',true)""", (uuid4(), safe))
        conn.execute("""INSERT INTO agent.tool_calls(id,run_id,tool_name,status,checkpointed)
            VALUES (%s,%s,'search','succeeded',true)""", (uuid4(), safe))
        assert operations.run_queue_summary()["oldest_claimable_created_at"] == safe_at

        queued_at = now - timedelta(minutes=15)
        queued = _run(team_id, queued_at, queue_deadline=now + timedelta(hours=1), conn=conn)
        stale = _run(team_id, now - timedelta(minutes=30),
                     queue_deadline=now - timedelta(minutes=1), conn=conn)
        live = _run(team_id, now - timedelta(minutes=40), status="running",
                    leased_until=now + timedelta(hours=1), execution_deadline=now + timedelta(hours=2), conn=conn)
        cancelling = _run(team_id, now - timedelta(minutes=50), status="cancelling", conn=conn)
        ids = unsafe_ids + [safe, queued, stale, live, cancelling]
        before = conn.execute("""SELECT id,status,leased_until,finished_at,error_code FROM agent.agent_runs
            WHERE id=ANY(%s) ORDER BY id""", (ids,)).fetchall()
        calls_before = conn.execute("""SELECT run_id,status,checkpointed FROM agent.model_calls WHERE run_id=ANY(%s)
            UNION ALL SELECT run_id,status,checkpointed FROM agent.tool_calls WHERE run_id=ANY(%s)""",
                                    (ids, ids)).fetchall()

        summary = operations.run_queue_summary()
        assert summary == {
            "queued_within_deadline": 1, "queued_past_deadline": 1,
            "running_lease_valid": 1, "running_lease_expired": 5,
            "cancelling": 1, "oldest_claimable_created_at": queued_at,
        }
        assert conn.execute("""SELECT id,status,leased_until,finished_at,error_code FROM agent.agent_runs
            WHERE id=ANY(%s) ORDER BY id""", (ids,)).fetchall() == before
        assert conn.execute("""SELECT run_id,status,checkpointed FROM agent.model_calls WHERE run_id=ANY(%s)
            UNION ALL SELECT run_id,status,checkpointed FROM agent.tool_calls WHERE run_id=ANY(%s)""",
                            (ids, ids)).fetchall() == calls_before
        conn.rollback()


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_run_lists_isolate_filter_paginate_and_hide_sensitive_fields():
    teams, headers = _identity()
    now = datetime.now(timezone.utc)
    tied = now - timedelta(hours=3)
    ids = [uuid4() for _ in range(3)]
    for run_id in ids:
        _run(teams[0], tied, run_id=run_id, task="sensitive task body")
    other = _run(teams[1], tied, task="other team's task")
    old = _run(teams[0], tied - timedelta(seconds=1), status="failed")
    with TestClient(agent_app) as client:
        assert client.get("/v1/runs", headers=headers[2]).status_code == 401
        assert client.get("/v1/admin/runs", headers=headers[0]).status_code == 401
        assert client.get("/v1/admin/runs/summary", headers=headers[0]).status_code == 401
        first = client.get("/v1/runs", headers=headers[0], params={"limit": 2})
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 2
        assert first.json()["next_cursor"]
        assert set(first.json()["items"][0]) == {
            "id", "mode", "status", "created_at", "started_at", "finished_at", "error_code"
        }
        second = client.get("/v1/runs", headers=headers[0], params={"limit": 2, "cursor": first.json()["next_cursor"]})
        assert second.status_code == 200, second.text
        seen = [UUID(item["id"]) for page in (first, second) for item in page.json()["items"]]
        assert seen == sorted(ids, reverse=True) + [old]
        assert other not in seen
        assert second.json()["next_cursor"] is None
        assert "sensitive task body" not in first.text + second.text
        assert "other team's task" not in first.text + second.text
        team_two = client.get("/v1/runs", headers=headers[1], params={"team_id": str(teams[0])})
        assert [UUID(item["id"]) for item in team_two.json()["items"]] == [other]

        admin = client.get("/v1/admin/runs", headers=headers[2], params={"team_id": str(teams[1])})
        assert admin.status_code == 200
        assert [UUID(item["id"]) for item in admin.json()["items"]] == [other]
        assert admin.json()["items"][0]["team_id"] == str(teams[1])
        filtered = client.get("/v1/admin/runs", headers=headers[2], params={
            "team_id": str(teams[0]), "status": "queued",
            "created_after": (tied - timedelta(seconds=1)).isoformat(),
            "created_before": (tied + timedelta(seconds=1)).isoformat(),
        })
        assert filtered.status_code == 200
        assert {UUID(item["id"]) for item in filtered.json()["items"]} == set(ids)
        assert [UUID(item["id"]) for item in client.get("/v1/runs", headers=headers[0], params={
            "created_after": tied.isoformat()
        }).json()["items"]] == []
        assert [UUID(item["id"]) for item in client.get("/v1/runs", headers=headers[0], params={
            "created_before": tied.isoformat()
        }).json()["items"]] == [old]
        for params in (
            {"cursor": "broken"},
            {"cursor": "a"},
            {"limit": 3, "cursor": first.json()["next_cursor"], "status": "queued"},
            {"cursor": first.json()["next_cursor"], "created_after": tied.isoformat()},
            {"cursor": first.json()["next_cursor"], "limit": 101},
            {"created_after": "2026-01-01T00:00:00"},
        ):
            assert client.get("/v1/runs", headers=headers[0], params=params).status_code == 422
        assert client.get("/v1/runs", headers=headers[1], params={"cursor": first.json()["next_cursor"]}).status_code == 422
        assert client.get("/v1/admin/runs", headers=headers[2], params={"cursor": first.json()["next_cursor"]}).status_code == 422


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_summary_distinguishes_stale_rows_without_settling_them():
    teams, headers = _identity()
    now = datetime.now(timezone.utc)
    with TestClient(agent_app) as client:
        baseline = client.get("/v1/admin/runs/summary", headers=headers[2]).json()
        valid = _run(teams[0], now - timedelta(minutes=5), queue_deadline=now + timedelta(hours=1))
        stale = _run(teams[0], now - timedelta(minutes=4), queue_deadline=now - timedelta(seconds=1))
        live = _run(teams[0], now - timedelta(minutes=3), status="running",
                    leased_until=now + timedelta(hours=1), execution_deadline=now + timedelta(hours=2))
        expired = _run(teams[0], now - timedelta(minutes=2), status="running",
                       leased_until=now - timedelta(seconds=1), execution_deadline=now + timedelta(hours=2))
        cancelling = _run(teams[0], now - timedelta(minutes=1), status="cancelling")
        ids = [valid, stale, live, expired, cancelling]
        with connect("AGENT_DATABASE_URL") as conn:
            before = conn.execute("SELECT id,status,finished_at,error_code FROM agent.agent_runs WHERE id=ANY(%s) ORDER BY id", (ids,)).fetchall()
        result = client.get("/v1/admin/runs/summary", headers=headers[2])
        assert result.status_code == 200, result.text
        summary = result.json()
        for field in ("queued_within_deadline", "queued_past_deadline", "running_lease_valid",
                      "running_lease_expired", "cancelling"):
            assert summary[field] == baseline[field] + 1
        assert datetime.fromisoformat(summary["oldest_claimable_created_at"]) <= now - timedelta(minutes=5)
        with connect("AGENT_DATABASE_URL") as conn:
            after = conn.execute("SELECT id,status,finished_at,error_code FROM agent.agent_runs WHERE id=ANY(%s) ORDER BY id", (ids,)).fetchall()
        assert after == before


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_readiness_live_db_missing_tables_bad_pointer_and_connection(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    with TestClient(agent_app) as agent, TestClient(rag_app) as rag:
        assert agent.get("/health").json() == {"status": "ok", "service": "agent"}
        assert rag.get("/health").json() == {"status": "ok", "service": "rag"}
        assert agent.get("/health/ready").json() == {"service": "agent", "status": "ok"}
        assert rag.get("/health/ready").json() == {"service": "rag", "status": "ok"}

        monkeypatch.setattr(readiness, "AGENT_TABLES", readiness.AGENT_TABLES + ("agent.absent_table",))
        assert agent.get("/health/ready").status_code == 503
        monkeypatch.setattr(readiness, "RAG_TABLES", readiness.RAG_TABLES + ("rag.absent_table",))
        assert rag.get("/health/ready").status_code == 503
        monkeypatch.setattr(readiness, "RAG_TABLES", readiness.RAG_TABLES[:-1])

        with connect("MIGRATION_DATABASE_URL") as conn:
            active_id = conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true").fetchone()["active_revision_id"]
            conn.execute("UPDATE rag.index_revisions SET state='superseded' WHERE id=%s", (active_id,))
        try:
            bad = rag.get("/health/ready")
            assert bad.status_code == 503
            assert bad.json() == {"service": "rag", "status": "unavailable"}
        finally:
            with connect("MIGRATION_DATABASE_URL") as conn:
                conn.execute("UPDATE rag.index_revisions SET state='active' WHERE id=%s", (active_id,))

        monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/invalid")
        assert agent.get("/health/ready").status_code == 503
        monkeypatch.setenv("RAG_DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/invalid")
        assert rag.get("/health/ready").status_code == 503
        assert agent.get("/health").status_code == rag.get("/health").status_code == 200
