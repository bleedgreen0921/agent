import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from agent_service.app import app as agent_app
from agent_service.rag_client import RagClient, RagError
from contracts.v1 import ErrorCode, ErrorResponse, EvidenceSearchRequest, EvidenceSearchResponse, RunCreate
from db.connection import connect
from identity.security import agent_team, new_key, rag_team, verify
from rag_service.app import app as rag_app
from rag_service.service_auth import require_service


def test_contracts_and_health():
    assert RunCreate(task="check").mode == "react"
    with pytest.raises(ValueError):
        RunCreate(task="check", team_id="fake")
    assert EvidenceSearchRequest(query="x").top_k == 5
    with pytest.raises(ValueError):
        EvidenceSearchRequest(query="x", top_k=21)
    with TestClient(rag_app) as rag, TestClient(agent_app) as agent:
        assert rag.get("/health").json() == {"status": "ok", "service": "rag"}
        assert agent.get("/health").json() == {"status": "ok", "service": "agent"}
        assert rag.post("/v1/evidence/search").status_code == 404
        assert agent.post("/v1/runs").status_code == 404
        response = rag.post("/v1/admin/teams", json={"name": "x"})
        assert response.status_code == 401
        assert ErrorResponse.model_validate(response.json()).error.code == ErrorCode.UNAUTHENTICATED


def test_service_bearer_is_separate(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    require_service("Bearer " + "s" * 43)
    with pytest.raises(Exception):
        require_service("Bearer key_abc.secret")


@pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
def test_migrations_permissions_and_key_lifecycle():
    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"]) as conn:
        for schema, version in (("identity", "identity_0001"), ("rag", "rag_0001"), ("agent", "agent_0001")):
            assert conn.execute(f"SELECT version_num FROM {schema}.alembic_version").fetchone()[0] == version
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    with connect("RAG_DATABASE_URL") as conn:
        assert conn.execute("SELECT count(*) AS n FROM identity.teams").fetchone()["n"] >= 0
        assert conn.execute("SELECT count(*) AS n FROM rag.documents").fetchone()["n"] >= 0
        doc_id = uuid4()
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'permission probe','restricted')", (doc_id,))
        conn.execute("DELETE FROM rag.documents WHERE id=%s", (doc_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO identity.teams(id, name) VALUES (%s, 'forbidden')", (uuid4(),))
    with connect("AGENT_DATABASE_URL") as conn:
        assert conn.execute("SELECT count(*) AS n FROM agent.runs").fetchone()["n"] >= 0
        run_id = uuid4()
        conn.execute("INSERT INTO agent.runs(id,team_id,key_id,task,mode) VALUES (%s,%s,'probe','probe','react')", (run_id, uuid4()))
        conn.execute("DELETE FROM agent.runs WHERE id=%s", (run_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM rag.documents")
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM rag.documents")

    admin_id, admin_raw, admin_digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.credentials(key_id,kind,digest) VALUES (%s,'admin',%s)", (admin_id, admin_digest))
    headers = {"Authorization": "Bearer " + admin_raw}
    with TestClient(rag_app) as client:
        team = client.post("/v1/admin/teams", headers=headers, json={"name": "test-" + uuid4().hex})
        assert team.status_code == 201
        team_id = team.json()["team_id"]
        first = client.post(f"/v1/admin/teams/{team_id}/keys", headers=headers, json={})
        second = client.post(f"/v1/admin/teams/{team_id}/keys", headers=headers, json={})
        assert first.status_code == second.status_code == 201
        key1, key2 = first.json()["key"], second.json()["key"]
        assert verify("Bearer " + key1, "team", "AGENT_DATABASE_URL").team_id == verify("Bearer " + key2, "team", "RAG_DATABASE_URL").team_id
        assert agent_team("Bearer " + key1).team_id == rag_team("Bearer " + key2).team_id
        assert client.post("/v1/admin/teams", headers={"Authorization": "Bearer " + key1}, json={"name": "wrong-type"}).status_code == 401
        with pytest.raises(Exception):
            verify("Bearer " + admin_raw, "team", "AGENT_DATABASE_URL")
        with pytest.raises(Exception):
            verify("Bearer " + key1, "admin", "RAG_DATABASE_URL")
        bad = key1.rsplit(".", 1)[0] + ".incorrect"
        with pytest.raises(Exception):
            verify("Bearer " + bad, "team", "AGENT_DATABASE_URL")
        with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
            row = conn.execute("SELECT digest FROM identity.credentials WHERE key_id=%s", (first.json()["key_id"],)).fetchone()
            assert bytes(row["digest"]) == hashlib.sha256(key1.split(".", 1)[1].encode()).digest()
            assert key1.encode() not in bytes(row["digest"])
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        assert client.put(f"/v1/admin/keys/{first.json()['key_id']}/expiry", headers=headers, json={"expires_at": past}).status_code == 200
        with pytest.raises(Exception):
            verify("Bearer " + key1, "team", "AGENT_DATABASE_URL")
        assert verify("Bearer " + key2, "team", "AGENT_DATABASE_URL").team_id is not None
        assert client.post(f"/v1/admin/keys/{second.json()['key_id']}/revoke", headers=headers).status_code == 200
        with pytest.raises(Exception):
            verify("Bearer " + key2, "team", "RAG_DATABASE_URL")


@pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
def test_rag_http_adapter_uses_persisted_team():
    team_id, run_id = uuid4(), uuid4()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, "adapter-" + uuid4().hex))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("INSERT INTO agent.runs(id,team_id,key_id,task,mode) VALUES (%s,%s,'key_test','task','react')", (run_id, team_id))
    def handler(request):
        assert request.headers["Authorization"] == "Bearer service-secret"
        assert request.headers["X-Team-Id"] == str(team_id)
        assert request.headers["X-Run-Id"] == str(run_id)
        assert request.headers["X-Tool-Call-Id"] == "call_1"
        assert request.url.path == "/v1/evidence/search"
        assert request.read() == b'{"query":"hello","top_k":5}'
        return httpx.Response(200, json={"request_id": "rag_req_1", "retrieval_id": "ret_1", "status": "no_hits", "evidences": [], "degradations": [], "new_field": 1})
    client = RagClient("http://rag.test", "service-secret", httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.search(run_id, "call_1", EvidenceSearchRequest(query="hello")).status == "no_hits"
    def error_handler(request):
        return httpx.Response(503, json={"error": {"code": "RAG_UNAVAILABLE", "message": "Unavailable"}, "request_id": "req_1"})
    client.client = httpx.Client(transport=httpx.MockTransport(error_handler))
    with pytest.raises(RagError) as exc:
        client.search(run_id, "call_1", EvidenceSearchRequest(query="hello"))
    assert exc.value.status == 503 and exc.value.code == ErrorCode.RAG_UNAVAILABLE
