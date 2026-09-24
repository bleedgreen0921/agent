import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from agent_service.app import app as agent_app
from agent_service.rag_client import RagClient, RagError, TrustedRunContext
from contracts.v1 import ErrorCode, ErrorResponse, EvidenceSearchRequest, RunCreate
from contracts.errors import ApiError, install_errors
from db.connection import connect
from db import migrate
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
        assert rag.post("/v1/evidence/search").status_code == 401
        assert agent.post("/v1/runs").status_code == 401
        response = rag.post("/v1/admin/teams", json={"name": "x"})
        assert response.status_code == 401
        assert ErrorResponse.model_validate(response.json()).error.code == ErrorCode.UNAUTHENTICATED


def test_service_bearer_is_separate(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    require_service("Bearer " + "s" * 43)
    with pytest.raises(Exception):
        require_service("Bearer key_abc.secret")


def test_api_errors_use_stable_contract():
    probe = FastAPI()
    install_errors(probe)

    @probe.get("/items/{item_id}")
    def item(item_id: int):
        return {"item_id": item_id}

    @probe.get("/forbidden")
    def forbidden():
        raise ApiError(403, ErrorCode.FORBIDDEN, "Forbidden")

    @probe.get("/unauthenticated")
    def unauthenticated():
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="api"', "X-Request-Id": "untrusted"},
        )

    @probe.get("/fail")
    def fail():
        raise RuntimeError("secret diagnostic")

    with TestClient(probe, raise_server_exceptions=False) as client:
        method_not_allowed = client.post("/items/1")
        unauthenticated_response = client.get("/unauthenticated")
        cases = (
            (client.get("/missing"), 404, ErrorCode.NOT_FOUND),
            (method_not_allowed, 405, ErrorCode.INVALID_REQUEST),
            (client.get("/items/not-an-integer"), 422, ErrorCode.INVALID_REQUEST),
            (client.get("/forbidden"), 403, ErrorCode.FORBIDDEN),
            (unauthenticated_response, 401, ErrorCode.UNAUTHENTICATED),
            (client.get("/fail"), 500, ErrorCode.INTERNAL_ERROR),
        )
    for response, status, code in cases:
        detail = ErrorResponse.model_validate(response.json())
        assert response.status_code == status
        assert detail.error.code == code
        assert response.headers["X-Request-Id"] == detail.request_id
    assert method_not_allowed.headers["Allow"] == "GET"
    assert unauthenticated_response.headers["WWW-Authenticate"] == 'Bearer realm="api"'
    assert unauthenticated_response.headers["X-Request-Id"] != "untrusted"
    assert "secret diagnostic" not in cases[-1][0].text


def test_migration_url_escapes_encoded_password(monkeypatch):
    captured = {}
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://owner:p%40ss%25word@db.test/platform")
    monkeypatch.setattr(migrate.command, "upgrade", lambda cfg, target: captured.update(url=cfg.get_main_option("sqlalchemy.url"), target=target))

    migrate.upgrade("identity")

    assert captured == {
        "url": "postgresql+psycopg://owner:p%40ss%25word@db.test/platform",
        "target": "head",
    }


def _call_rag(client: RagClient, operation: str, run_id):
    if operation == "search":
        return client.search(run_id, "call_1", EvidenceSearchRequest(query="hello"))
    return client.read(run_id, "call_1", "evidence_1")


@pytest.mark.parametrize("operation", ["search", "read"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_rag_adapter_translates_transport_errors(monkeypatch, operation, error_type):
    run_id, team_id = uuid4(), uuid4()
    monkeypatch.setattr(TrustedRunContext, "from_persisted_run", classmethod(lambda cls, value: cls(value, team_id)))

    def handler(request):
        raise error_type("RAG unavailable", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = RagClient("http://rag.test", "service-secret", http_client)
    with http_client, pytest.raises(RagError) as exc:
        _call_rag(client, operation, run_id)
    assert exc.value.status == 503
    assert exc.value.code == ErrorCode.RAG_UNAVAILABLE


@pytest.mark.parametrize("operation", ["search", "read"])
@pytest.mark.parametrize(
    ("status", "content", "expected_status", "expected_code"),
    [
        (503, b"upstream unavailable", 503, ErrorCode.RAG_UNAVAILABLE),
        (401, b"unauthorized", 401, ErrorCode.UNAUTHENTICATED),
        (429, b'{"error":{"code":"QUOTA_EXCEEDED","message":"Quota exceeded"},"request_id":"req_1"}', 429, ErrorCode.QUOTA_EXCEEDED),
        (200, b'{"unexpected":true}', 502, ErrorCode.RAG_UNAVAILABLE),
    ],
)
def test_rag_adapter_translates_non_contract_responses(monkeypatch, operation, status, content, expected_status, expected_code):
    run_id, team_id = uuid4(), uuid4()
    monkeypatch.setattr(TrustedRunContext, "from_persisted_run", classmethod(lambda cls, value: cls(value, team_id)))
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, content=content)))
    client = RagClient("http://rag.test", "service-secret", http_client)

    with http_client, pytest.raises(RagError) as exc:
        _call_rag(client, operation, run_id)

    assert exc.value.status == expected_status
    assert exc.value.code == expected_code


def test_rag_adapter_default_client_has_no_timeout():
    client = RagClient("http://rag.test", "service-secret")
    try:
        assert client.client.timeout.connect is None
        assert client.client.timeout.read is None
        assert client.client.timeout.write is None
        assert client.client.timeout.pool is None
    finally:
        client.client.close()


@pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
def test_migrations_permissions_and_key_lifecycle():
    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"]) as conn:
        for schema, version in (("identity", "identity_0001"), ("rag", "rag_0002"), ("agent", "agent_0003")):
            assert conn.execute(f"SELECT version_num FROM {schema}.alembic_version").fetchone()[0] == version
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    with connect("RAG_DATABASE_URL") as conn:
        assert conn.execute("SELECT count(*) AS n FROM identity.teams").fetchone()["n"] >= 0
        assert conn.execute("SELECT count(*) AS n FROM rag.documents").fetchone()["n"] >= 0
        privileges = conn.execute(
            "SELECT has_schema_privilege(current_user, 'public', 'USAGE') AS can_use, "
            "has_schema_privilege(current_user, 'public', 'CREATE') AS can_create"
        ).fetchone()
        assert privileges == {"can_use": True, "can_create": False}
        assert conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector AS distance").fetchone()["distance"] == 1.0
        doc_id = uuid4()
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'permission probe','restricted')", (doc_id,))
        conn.execute("DELETE FROM rag.documents WHERE id=%s", (doc_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO identity.teams(id, name) VALUES (%s, 'forbidden')", (uuid4(),))
    with connect("AGENT_DATABASE_URL") as conn:
        assert conn.execute("SELECT count(*) AS n FROM agent.agent_runs").fetchone()["n"] >= 0
        run_id = uuid4()
        conn.execute("INSERT INTO agent.agent_runs(id,team_id,key_id,task,mode,request_digest,queue_deadline_at) VALUES (%s,%s,'probe','probe','react',decode(repeat('00',32),'hex'),now()+interval '1 minute')", (run_id, uuid4()))
        conn.execute("DELETE FROM agent.agent_runs WHERE id=%s", (run_id,))
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

    concurrent_name = "concurrent-" + uuid4().hex

    def create_same_team():
        with TestClient(rag_app) as concurrent_client:
            return concurrent_client.post("/v1/admin/teams", headers=headers, json={"name": concurrent_name})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: create_same_team(), range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert ErrorResponse.model_validate(conflict.json()).error.code == ErrorCode.IDEMPOTENCY_CONFLICT
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        assert conn.execute("SELECT count(*) AS n FROM identity.teams WHERE name = %s", (concurrent_name,)).fetchone()["n"] == 1


@pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
def test_rag_http_adapter_uses_persisted_team():
    team_id, run_id = uuid4(), uuid4()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, "adapter-" + uuid4().hex))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("INSERT INTO agent.agent_runs(id,team_id,key_id,task,mode,request_digest,queue_deadline_at) VALUES (%s,%s,'key_test','task','react',decode(repeat('00',32),'hex'),now()+interval '1 minute')", (run_id, team_id))
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
    def read_handler(request):
        assert request.url.path == "/v1/evidence/ev_1"
        assert request.headers["X-Team-Id"] == str(team_id)
        return httpx.Response(200, json={"evidence_id": "ev_1", "document_id": "doc_1", "document_version_id": "ver_1", "title": "Title", "content": "Text", "source_locator": {"kind": "txt", "line_start": 1, "line_end": 1}})
    client.client = httpx.Client(transport=httpx.MockTransport(read_handler))
    assert client.read(run_id, "call_2", "ev_1").content == "Text"
    def error_handler(request):
        return httpx.Response(503, json={"error": {"code": "RAG_UNAVAILABLE", "message": "Unavailable"}, "request_id": "req_1"})
    client.client = httpx.Client(transport=httpx.MockTransport(error_handler))
    with pytest.raises(RagError) as exc:
        client.search(run_id, "call_1", EvidenceSearchRequest(query="hello"))
    assert exc.value.status == 503 and exc.value.code == ErrorCode.RAG_UNAVAILABLE
