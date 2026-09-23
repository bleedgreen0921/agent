import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from db.connection import connect
from identity.security import new_key
from rag_service.app import app
from rag_service.chunking import ChunkDraft
from rag_service import retrieval, worker


pytestmark = pytest.mark.skipif(not os.environ.get("RAG_DATABASE_URL"), reason="isolated PostgreSQL not configured")


def _admin_and_teams():
    key_id, key, digest = new_key()
    teams = [uuid4(), uuid4()]
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.credentials(key_id,kind,digest) VALUES (%s,'admin',%s)", (key_id, digest))
        for team_id in teams:
            conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, "rag-test-" + uuid4().hex))
    return {"Authorization": "Bearer " + key}, teams


def _service(team):
    return {"Authorization": "Bearer " + "s" * 43, "X-Team-Id": str(team), "X-Run-Id": "run_test", "X-Tool-Call-Id": "call_test"}


def test_document_lifecycle_acl_and_degradation(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_FILES_DIR", str(tmp_path))
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "s" * 43)
    monkeypatch.setattr(worker, "make_chunks", lambda blocks, title: [ChunkDraft(blocks[0].text, (), blocks[0].locator)])
    monkeypatch.setattr(worker, "embed", lambda texts, model, dimensions: [[0.1] * dimensions for _ in texts])
    monkeypatch.setattr(retrieval, "embed", lambda texts, model, dimensions: [[0.1] * dimensions for _ in texts])
    monkeypatch.setattr(retrieval, "rerank", lambda query, contents: list(range(len(contents))))
    monkeypatch.setattr(retrieval, "rewrite", lambda query: query)
    admin, teams = _admin_and_teams()
    with TestClient(app) as client:
        create_headers = {**admin, "Idempotency-Key": "create-" + uuid4().hex}
        create_data = {"title": "Secret", "visibility": "restricted", "team_ids": f'["{teams[0]}"]'}
        upload = client.post("/v1/admin/documents", headers=create_headers, data=create_data, files={"file": ("a.txt", b"alpha evidence", "text/plain")})
        assert upload.status_code == 202, upload.text
        doc_id, version_id = upload.json()["document_id"], upload.json()["document_version_id"]
        replay_create = client.post("/v1/admin/documents", headers=create_headers, data=create_data, files={"file": ("renamed.txt", b"alpha evidence", "text/plain")})
        assert replay_create.json()["document_id"] == doc_id
        create_conflict = client.post("/v1/admin/documents", headers=create_headers, data={**create_data, "visibility": "public"}, files={"file": ("a.txt", b"alpha evidence", "text/plain")})
        assert create_conflict.status_code == 409
        assert client.get(f"/v1/admin/documents/{doc_id}/versions/{version_id}", headers=admin).json()["status"] == "pending"
        assert worker.run_once()
        assert client.get(f"/v1/admin/documents/{doc_id}/versions/{version_id}", headers=admin).json()["status"] == "active"
        search = client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "alpha"})
        assert search.status_code == 200, search.text
        evidence = search.json()["evidences"][0]
        assert evidence["document_version_id"] == "ver_" + version_id
        assert client.get("/v1/evidence/" + evidence["evidence_id"], headers=_service(teams[0])).status_code == 200
        assert client.get("/v1/evidence/" + evidence["evidence_id"], headers=_service(teams[1])).status_code == 404
        assert client.post("/v1/evidence/search", headers=_service(teams[1]), json={"query": "alpha"}).json()["status"] == "no_hits"
        from rag_service.models import ModelFailure
        with monkeypatch.context() as patch:
            patch.setattr(retrieval, "embed", lambda *args: (_ for _ in ()).throw(ModelFailure("EMBEDDING_UNAVAILABLE")))
            degraded = client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "alpha"})
            assert degraded.status_code == 200
            assert degraded.json()["status"] == "degraded"
            assert "DENSE_UNAVAILABLE" in degraded.json()["degradations"]
            patch.setattr(retrieval, "_sparse", lambda *args: (_ for _ in ()).throw(__import__("psycopg").OperationalError("temporary")))
            unavailable = client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "alpha"})
            assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "RAG_UNAVAILABLE"

        second = client.post(f"/v1/admin/documents/{doc_id}/versions", headers={**admin, "Idempotency-Key": "next"}, files={"file": ("a.txt", b"beta evidence", "text/plain")})
        assert second.status_code == 202
        replay = client.post(f"/v1/admin/documents/{doc_id}/versions", headers={**admin, "Idempotency-Key": "next"}, files={"file": ("a.txt", b"beta evidence", "text/plain")})
        assert replay.json()["document_version_id"] == second.json()["document_version_id"]
        conflict = client.post(f"/v1/admin/documents/{doc_id}/versions", headers={**admin, "Idempotency-Key": "next"}, files={"file": ("a.txt", b"other", "text/plain")})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "alpha"}).json()["evidences"]
        assert worker.run_once()
        current = client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "alpha"}).json()
        assert current["evidences"][0]["document_version_id"] == "ver_" + second.json()["document_version_id"]
        invalid = client.post(f"/v1/admin/documents/{doc_id}/versions", headers=admin, files={"file": ("a.txt", b"\n", "text/plain")})
        assert invalid.status_code == 202
        assert worker.run_once()
        assert client.get(f"/v1/admin/documents/{doc_id}/versions/{invalid.json()['document_version_id']}", headers=admin).json()["error_code"] == "EMPTY_CONTENT"
        preserved = client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "beta"}).json()
        assert preserved["evidences"][0]["document_version_id"] == "ver_" + second.json()["document_version_id"]
        transient = client.post(f"/v1/admin/documents/{doc_id}/versions", headers=admin, files={"file": ("a.txt", b"gamma evidence", "text/plain")})
        with monkeypatch.context() as patch:
            patch.setattr(worker, "embed", lambda *args: (_ for _ in ()).throw(ModelFailure("EMBEDDING_UNAVAILABLE")))
            for attempt in range(1, 4):
                with connect("RAG_DATABASE_URL") as conn:
                    conn.execute("UPDATE rag.processing_jobs SET available_at=now() WHERE version_id=%s", (UUID(transient.json()["document_version_id"]),))
                assert worker.run_once()
                state = client.get(f"/v1/admin/documents/{doc_id}/versions/{transient.json()['document_version_id']}", headers=admin).json()
                assert state["attempts"] == attempt
                assert state["status"] == ("failed" if attempt == 3 else "pending")
        assert client.post("/v1/evidence/search", headers=_service(teams[0]), json={"query": "beta"}).json()["evidences"][0]["document_version_id"] == "ver_" + second.json()["document_version_id"]
        duplicate_headers = {**admin, "Idempotency-Key": "duplicate-" + uuid4().hex}
        duplicate = client.post(f"/v1/admin/documents/{doc_id}/versions", headers=duplicate_headers, files={"file": ("renamed.txt", b"beta evidence", "text/plain")})
        assert duplicate.json()["document_version_id"] == second.json()["document_version_id"]
        duplicate_conflict = client.post(f"/v1/admin/documents/{doc_id}/versions", headers=duplicate_headers, files={"file": ("a.txt", b"different", "text/plain")})
        assert duplicate_conflict.status_code == 409
        assert client.get("/v1/evidence/" + evidence["evidence_id"], headers=_service(teams[0])).status_code == 200
        assert client.put(f"/v1/admin/documents/{doc_id}/access", headers=admin, json={"visibility": "restricted", "team_ids": [str(teams[1])]}).status_code == 200
        assert client.get("/v1/evidence/" + evidence["evidence_id"], headers=_service(teams[0])).status_code == 404
        assert client.post("/v1/evidence/search", headers=_service(teams[1]), json={"query": "beta"}).json()["evidences"]
        assert client.delete(f"/v1/admin/documents/{doc_id}", headers=admin).status_code == 200
        assert client.post("/v1/evidence/search", headers=_service(teams[1]), json={"query": "beta"}).json()["status"] == "no_hits"


def test_shadow_revision_switch_requires_coverage(monkeypatch):
    from rag_service import reindex
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(reindex, "embed", lambda texts, model, dimensions: [[0.2] * dimensions for _ in texts])
    document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4()
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'shadow probe','public')", (document_id,))
        conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
            VALUES (%s,%s,1,%s,'/unused','text/plain','active')""", (version_id, document_id, bytes(32)))
        conn.execute("UPDATE rag.documents SET active_version_id=%s WHERE id=%s", (version_id, document_id))
        conn.execute("""INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,source_locator,search_text)
            VALUES (%s,%s,%s,1,'shadow content',%s,'shadow content')""", (chunk_id, document_id, version_id, Jsonb({"kind": "txt", "line_start": 1, "line_end": 1})))

    revision = reindex.start("Qwen3-Embedding-0.6B", 1024)
    try:
        with pytest.raises(ValueError, match="missing"):
            reindex.switch(revision)
        assert reindex.build(revision) >= 1
        reindex.switch(revision)
        with connect("RAG_DATABASE_URL") as conn:
            assert conn.execute("SELECT active_revision_id FROM rag.index_state").fetchone()["active_revision_id"] == revision
    finally:
        with connect("RAG_DATABASE_URL") as conn:
            conn.execute("UPDATE rag.documents SET active_version_id=NULL WHERE id=%s", (document_id,))
            conn.execute("DELETE FROM rag.chunks WHERE document_id=%s", (document_id,))
            conn.execute("DELETE FROM rag.document_versions WHERE id=%s", (version_id,))
            conn.execute("DELETE FROM rag.documents WHERE id=%s", (document_id,))
            conn.execute("DELETE FROM rag.index_revisions WHERE id=%s AND state='building'", (revision,))
