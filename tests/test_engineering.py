"""Failure boundaries and Worker status without real model or document data."""

import os
import threading
import time
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from agent_service.app import app as agent_app
from agent_service import memory
from agent_service import worker as agent_worker
from db.config_check import validate_role
from db.connection import bounded_conninfo, connect
from db.worker_status import beat, remove, snapshot
from identity.security import new_key
from rag_service import worker
from rag_service.app import app as rag_app
from rag_service.chunking import ChunkDraft


HAS_DATABASE = all(os.environ.get(name) for name in (
    "AGENT_DATABASE_URL", "RAG_DATABASE_URL", "IDENTITY_ADMIN_DATABASE_URL"
))


def _delete_document_probe(document_id):
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("UPDATE rag.documents SET active_version_id=NULL WHERE id=%s", (document_id,))
        conn.execute("DELETE FROM rag.processing_jobs WHERE version_id IN (SELECT id FROM rag.document_versions WHERE document_id=%s)", (document_id,))
        conn.execute("DELETE FROM rag.chunks WHERE document_id=%s", (document_id,))
        conn.execute("DELETE FROM rag.document_versions WHERE document_id=%s", (document_id,))
        conn.execute("DELETE FROM rag.documents WHERE id=%s", (document_id,))


def sleeping_child(_job, _outcome):
    time.sleep(30)


def successful_child(_job, outcome):
    outcome.put((None, False))


def crashing_child(_job, _outcome):
    os._exit(7)


def test_database_profiles_preserve_dsn_options_and_bound_waits(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DB_CONTROL_STATEMENT_TIMEOUT_SECONDS", "15")
    dsn, timeout = bounded_conninfo("postgresql://user:secret@localhost/db?options=-c%20search_path%3Dagent", "control")
    settings = conninfo_to_dict(dsn)
    assert timeout == 10
    assert "search_path=agent" in settings["options"]
    assert "statement_timeout=15000" in settings["options"]
    assert "lock_timeout=5000" in settings["options"]
    monkeypatch.setenv("DB_CONTROL_STATEMENT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="DB_CONTROL_STATEMENT_TIMEOUT_SECONDS"):
        bounded_conninfo(dsn, "control")


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_database_statement_wait_is_bounded(monkeypatch):
    monkeypatch.setenv("DB_CONTROL_STATEMENT_TIMEOUT_SECONDS", "1")
    with pytest.raises(psycopg.errors.QueryCanceled):
        with connect("AGENT_DATABASE_URL", profile="control") as conn:
            conn.execute("SELECT pg_sleep(2)")


def test_role_configuration_fails_early_and_keeps_optional_models_optional(monkeypatch):
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://user:secret@localhost/db")
    monkeypatch.setenv("AGENT_MODEL_URL", "http://model.test")
    monkeypatch.setenv("AGENT_MODEL", "mock")
    monkeypatch.setenv("RAG_BASE_URL", "http://rag.test")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    validate_role("agent-worker")
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "60")
    with pytest.raises(ValueError, match="AGENT_HEARTBEAT_SECONDS"):
        validate_role("agent-worker")
    monkeypatch.setenv("AGENT_HEARTBEAT_SECONDS", "30")
    monkeypatch.setenv("AGENT_MODEL_URL", "ftp://model.test")
    with pytest.raises(ValueError, match="AGENT_MODEL_URL"):
        validate_role("agent-worker")


def test_document_supervisor_terminates_blocked_child_at_deadline(monkeypatch):
    failures = []
    monkeypatch.setattr(worker, "fail_safely", lambda job, code, retryable: failures.append((code, retryable)))
    monkeypatch.setattr(worker, "heartbeat", lambda _job: True)
    presence = worker.Presence()
    monkeypatch.setattr(presence, "touch", lambda: None)
    started = time.monotonic()
    worker.supervise({"id": uuid4(), "remaining_seconds": 0.3}, presence, child_target=sleeping_child)
    assert time.monotonic() - started < 5
    assert failures == [("PROCESSING_TIMEOUT", False)]


@pytest.mark.parametrize("target,expected", [
    (successful_child, []),
    (crashing_child, [("WORKER_INTERRUPTED", True)]),
])
def test_document_supervisor_handles_child_exit(monkeypatch, target, expected):
    failures = []
    monkeypatch.setattr(worker, "fail_safely", lambda job, code, retryable: failures.append((code, retryable)))
    presence = worker.Presence()
    monkeypatch.setattr(presence, "touch", lambda: None)
    worker.supervise({"id": uuid4(), "remaining_seconds": 5}, presence, child_target=target)
    assert failures == expected


def test_agent_worker_database_failure_terminates_children_and_retries(monkeypatch):
    processes = []

    class FakeProcess:
        def __init__(self, *, name, **_kwargs):
            self.name = name
            self.alive = False
            self.terminated = False
            processes.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            self.terminated = True

        def join(self, **_kwargs):
            pass

    class FakeContext:
        Process = FakeProcess

    class StopWorker(Exception):
        pass

    claimed = {"run_id": uuid4(), "lease_token": uuid4()}
    outcomes = iter((claimed, psycopg.OperationalError("unavailable"), StopWorker()))

    def claim():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    sleeps = []
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setattr(agent_worker, "validate_role", lambda _role: None)
    monkeypatch.setattr(agent_worker.mp, "get_context", lambda _method: FakeContext())
    monkeypatch.setattr(agent_worker, "claim_run", claim)
    monkeypatch.setattr(agent_worker, "beat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent_worker, "remove", lambda *_args: None)
    monkeypatch.setattr(agent_worker.time, "sleep", sleeps.append)
    with pytest.raises(StopWorker):
        agent_worker.main()
    assert sleeps == [1.0]
    assert any(process.name.startswith("agent-run-") and process.terminated for process in processes)


def test_memory_worker_backs_off_after_database_failure(monkeypatch):
    class StopWorker(Exception):
        pass

    outcomes = iter((psycopg.OperationalError("unavailable"), False, StopWorker()))

    def next_job():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    sleeps = []
    monkeypatch.setattr(memory, "process_next_job", next_job)
    monkeypatch.setattr(memory.time, "sleep", sleeps.append)
    with pytest.raises(StopWorker):
        memory.main()
    assert sleeps == [1.0, 1.0]


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_worker_heartbeats_and_admin_status_are_read_only(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    key_id, key, digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.credentials(key_id,kind,digest) VALUES (%s,'admin',%s)", (key_id, digest))
    headers = {"Authorization": "Bearer " + key}
    ids = {"agent": uuid4(), "rag": uuid4()}
    try:
        beat("agent", ids["agent"], memory_child_alive=True)
        beat("rag", ids["rag"])
        for service, app in (("agent", agent_app), ("rag", rag_app)):
            state = snapshot(service)
            assert state["online_count"] >= 1
            with TestClient(app) as client:
                assert client.get("/v1/admin/workers").status_code == 401
                response = client.get("/v1/admin/workers", headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["service"] == service
                assert any(row["instance_id"] == str(ids[service]) for row in response.json()["instances"])
        with connect("AGENT_DATABASE_URL") as conn:
            conn.execute("UPDATE agent.worker_instances SET heartbeat_at=now()-interval '2 minutes' WHERE instance_id=%s", (ids["agent"],))
        assert any(not row["online"] for row in snapshot("agent")["instances"] if row["instance_id"] == ids["agent"])
    finally:
        for service, instance_id in ids.items():
            remove(service, instance_id)


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_document_timeout_is_terminal_even_for_retryable_failure():
    document_id, version_id, job_id, token = (uuid4() for _ in range(4))
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'timeout probe','public')", (document_id,))
        conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
            VALUES (%s,%s,1,%s,'/unused','text/plain','processing')""", (version_id, document_id, bytes(32)))
        conn.execute("""INSERT INTO rag.processing_jobs(id,version_id,status,attempts,lease_token,leased_until,deadline_at)
            VALUES (%s,%s,'running',1,%s,now()+interval '1 minute',now()-interval '1 second')""",
            (job_id, version_id, token))
    worker.fail({"id": job_id, "version_id": version_id, "token": token, "attempts": 1}, "DATABASE_UNAVAILABLE", True)
    with connect("RAG_DATABASE_URL") as conn:
        job = conn.execute("SELECT status,error_code FROM rag.processing_jobs WHERE id=%s", (job_id,)).fetchone()
        version = conn.execute("SELECT status,error_code FROM rag.document_versions WHERE id=%s", (version_id,)).fetchone()
    assert job == version == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    _delete_document_probe(document_id)


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_document_deadline_survives_retry_and_expired_job_is_settled(monkeypatch):
    document_id, version_id, job_id = (uuid4() for _ in range(3))
    monkeypatch.setenv("RAG_DOCUMENT_TIMEOUT_SECONDS", "14400")
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'deadline probe','public')", (document_id,))
        conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
            VALUES (%s,%s,1,%s,'/unused','text/plain','pending')""", (version_id, document_id, bytes(32)))
        conn.execute("""INSERT INTO rag.processing_jobs(id,version_id,status,available_at)
            VALUES (%s,%s,'queued','2000-01-01')""", (job_id, version_id))
    first = worker.claim()
    assert first["id"] == job_id
    assert 14300 < first["remaining_seconds"] <= 14400
    worker.fail(first, "DATABASE_UNAVAILABLE", True)
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("UPDATE rag.processing_jobs SET available_at='2000-01-01' WHERE id=%s", (job_id,))
    second = worker.claim()
    assert second["id"] == job_id
    assert second["deadline_at"] == first["deadline_at"]
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("UPDATE rag.processing_jobs SET deadline_at=now()-interval '1 second' WHERE id=%s", (job_id,))
    worker.claim()
    with connect("RAG_DATABASE_URL") as conn:
        job = conn.execute("SELECT status,error_code FROM rag.processing_jobs WHERE id=%s", (job_id,)).fetchone()
        version = conn.execute("SELECT status,error_code FROM rag.document_versions WHERE id=%s", (version_id,)).fetchone()
    assert job == version == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    _delete_document_probe(document_id)


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
@pytest.mark.parametrize("expire_before_commit", [False, True])
def test_document_publish_allows_heartbeat_before_final_lease_check(monkeypatch, expire_before_commit):
    document_id, version_id, job_id, token = (uuid4() for _ in range(4))
    entered = threading.Event()
    release = threading.Event()
    errors = []
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'publish probe','public')", (document_id,))
        conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
            VALUES (%s,%s,1,%s,'/unused','text/plain','processing')""", (version_id, document_id, bytes(32)))
        conn.execute("""INSERT INTO rag.processing_jobs(id,version_id,status,attempts,lease_token,leased_until,deadline_at)
            VALUES (%s,%s,'running',1,%s,now()+interval '1 minute',now()+interval '5 minutes')""",
            (job_id, version_id, token))
    job = {"id": job_id, "version_id": version_id, "token": token}
    monkeypatch.setattr(worker, "parse_file", lambda *_args: ([], []))
    monkeypatch.setattr(worker, "make_chunks", lambda *_args: [ChunkDraft("publish content", (), {"kind": "txt", "line_start": 1, "line_end": 1})])
    monkeypatch.setattr(worker, "embed", lambda texts, _model, dimensions: [[0.1] * dimensions for _ in texts])

    def pause_inside_publish(_content):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("Test did not release publisher")
        return "publish content"

    monkeypatch.setattr(worker, "sparse_terms", pause_inside_publish)

    def publish():
        try:
            worker.process(job)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert worker.heartbeat(job)
        assert time.monotonic() - started < 3
        if expire_before_commit:
            with connect("RAG_DATABASE_URL") as conn:
                conn.execute("UPDATE rag.processing_jobs SET deadline_at=now()-interval '1 second' WHERE id=%s", (job_id,))
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    with connect("RAG_DATABASE_URL") as conn:
        status = conn.execute("SELECT status FROM rag.processing_jobs WHERE id=%s", (job_id,)).fetchone()["status"]
        chunks = conn.execute("SELECT count(*) AS n FROM rag.chunks WHERE version_id=%s", (version_id,)).fetchone()["n"]
    if expire_before_commit:
        assert len(errors) == 1 and isinstance(errors[0], worker.ModelFailure)
        assert status == "running" and chunks == 0
        worker.fail({**job, "attempts": 1}, "PROCESSING_TIMEOUT", False)
    else:
        assert not errors
        assert status == "done" and chunks == 1
    _delete_document_probe(document_id)
