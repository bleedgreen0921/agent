import hashlib
import json
import os
import types
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest

from agent_service import manifest as manifest_module
from agent_service import runtime
from agent_service.runs import timeline
from contracts.errors import ApiError
from contracts.v1 import RunTimeline
from db.connection import connect
from db.doctor import render_text, run_checks
from rag_service import retrieval
from rag_service.models import ModelFailure


HAS_DATABASE = bool(os.environ.get("IDENTITY_ADMIN_DATABASE_URL"))


def test_manifest_revision_sources_and_secret_exclusion(monkeypatch):
    manifest_module.application_revision.cache_clear()
    monkeypatch.setenv("APP_REVISION", " release-123 ")
    monkeypatch.setenv("AGENT_MODEL_URL", "https://model.example.invalid/v1")
    monkeypatch.setenv("AGENT_MODEL_KEY", "model-secret")
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "rag-secret")
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    value = manifest_module.execution_manifest()
    assert value["application"] == {"revision": "release-123", "revision_source": "environment", "dirty": False}
    encoded = json.dumps(value)
    assert "model-secret" not in encoded
    assert "rag-secret" not in encoded
    assert "model.example.invalid" not in encoded
    assert value["model"]["base_url_sha256"] == hashlib.sha256(b"https://model.example.invalid/v1").hexdigest()

    monkeypatch.delenv("APP_REVISION")
    manifest_module.application_revision.cache_clear()
    outputs = iter([types.SimpleNamespace(stdout="abc123\n"), types.SimpleNamespace(stdout=" M tracked.py\n")])
    monkeypatch.setattr(manifest_module.subprocess, "run", lambda *args, **kwargs: next(outputs))
    assert manifest_module.application_revision() == {"revision": "abc123", "revision_source": "git", "dirty": True}

    manifest_module.application_revision.cache_clear()
    monkeypatch.setattr(manifest_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert manifest_module.application_revision() == {"revision": "unknown", "revision_source": "unknown", "dirty": False}
    manifest_module.application_revision.cache_clear()


def test_doctor_missing_configuration_is_structured(monkeypatch):
    for name in ("MIGRATION_DATABASE_URL", "RAG_DATABASE_URL", "AGENT_DATABASE_URL", "IDENTITY_ADMIN_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    report = run_checks()
    assert report["schema_version"] == 1
    assert report["status"] == "error"
    required = next(item for item in report["checks"] if item["code"] == "config.required_env")
    assert required["status"] == "error"
    text = render_text(report)
    assert "[ERROR] config.required_env:" in text
    assert "postgresql://" not in json.dumps(report)


def _quiesce_runs():
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("""UPDATE agent.agent_runs SET status='cancelled',finished_at=now(),
            termination_reason='enhancement_test_cleanup',lease_token=NULL,leased_until=NULL
            WHERE status IN ('queued','running','cancelling')""")


def _queued_run():
    team_id, run_id = uuid4(), uuid4()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        conn.execute("INSERT INTO identity.teams(id,name) VALUES (%s,%s)", (team_id, "enhancement-" + uuid4().hex))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("""INSERT INTO agent.agent_runs(
            id,team_id,key_id,task,mode,status,request_digest,queue_deadline_at,created_at)
            VALUES (%s,%s,'enhancement-key','private task','react','queued',%s,now()+interval '5 minutes',now()-interval '1 day')""",
            (run_id, team_id, hashlib.sha256(str(run_id).encode()).digest()),
        )
    return team_id, run_id


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_manifest_claim_is_atomic_immutable_and_least_privilege(monkeypatch):
    _quiesce_runs()
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "1")
    monkeypatch.setattr(runtime, "execution_manifest", lambda: {"marker": "first"})
    _, run_id = _queued_run()
    claimed = runtime.claim_run()
    assert claimed and claimed["run_id"] == run_id
    with connect("AGENT_DATABASE_URL") as conn:
        stored = conn.execute("SELECT schema_version,manifest FROM agent.run_manifests WHERE run_id=%s", (run_id,)).fetchone()
        conn.execute("UPDATE agent.agent_runs SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    assert stored == {"schema_version": 1, "manifest": {"marker": "first"}}

    monkeypatch.setattr(runtime, "execution_manifest", lambda: {"marker": "second"})
    resumed = runtime.claim_run()
    assert resumed and resumed["run_id"] == run_id and not resumed["first_claim"]
    with connect("AGENT_DATABASE_URL") as conn:
        assert conn.execute("SELECT manifest FROM agent.run_manifests WHERE run_id=%s", (run_id,)).fetchone()["manifest"] == {"marker": "first"}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agent.run_manifests SET manifest='{}'::jsonb WHERE run_id=%s", (run_id,))

    _quiesce_runs()
    _, failed_run_id = _queued_run()
    monkeypatch.setattr(runtime, "execution_manifest", lambda: {"not_json": {1}})
    with pytest.raises(TypeError):
        runtime.claim_run()
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT status,started_at FROM agent.agent_runs WHERE id=%s", (failed_run_id,)).fetchone()
        captured = conn.execute("SELECT 1 FROM agent.run_manifests WHERE run_id=%s", (failed_run_id,)).fetchone()
    assert row == {"status": "queued", "started_at": None}
    assert captured is None


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_timeline_is_deterministic_linked_and_safe(monkeypatch):
    _quiesce_runs()
    monkeypatch.setenv("AGENT_MAX_CONCURRENT_RUNS", "1")
    monkeypatch.setattr(runtime, "execution_manifest", lambda: {"safe": True})
    _, run_id = _queued_run()
    claimed = runtime.claim_run()
    model_id = runtime.reserve_model(run_id, claimed["lease_token"], "react", "mock", input_summary={"message_count": 1})
    assert runtime.settle_model(run_id, claimed["lease_token"], model_id, "succeeded", output_summary={"message_count": 1})
    tool_id = runtime.reserve_tool(run_id, claimed["lease_token"], "search_evidence", {"arguments_sha256": "abc"}, triggering_model_call_id=model_id)
    assert runtime.settle_tool(
        run_id,
        claimed["lease_token"],
        tool_id,
        "succeeded",
        service_request_id="rag_req_safe",
        retrieval_id="ret_safe",
        evidence_ids=["ev_safe"],
        result_summary={"evidence_count": 1},
    )
    draft = {"answer": "private answer", "claims": [], "citations": [], "notices": [{"code": "N", "message": "safe"}]}
    assert runtime.publish_result(run_id, claimed["lease_token"], draft, {}, False, "model_finished")
    fixed = datetime.now(timezone.utc) - timedelta(minutes=1)
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.model_calls SET started_at=%s WHERE id=%s", (fixed, model_id))
        conn.execute("UPDATE agent.tool_calls SET started_at=%s WHERE id=%s", (fixed, tool_id))

    payload = timeline(run_id)
    parsed = RunTimeline.model_validate(payload)
    types_seen = [event.type for event in parsed.events]
    assert types_seen.index("model_call") < types_seen.index("tool_call")
    tool = next(event for event in parsed.events if event.type == "tool_call")
    assert tool.parent_id == str(model_id)
    assert tool.details["retrieval_id"] == "ret_safe"
    assert tool.details["evidence_ids"] == ["ev_safe"]
    published = next(event for event in parsed.events if event.type == "result_published")
    assert published.details == {"claim_count": 0, "citation_count": 0, "notice_count": 1}
    encoded = json.dumps(payload, default=str)
    assert "private task" not in encoded
    assert "private answer" not in encoded

    _, legacy_id = _queued_run()
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("UPDATE agent.agent_runs SET status='cancelled',finished_at=now(),termination_reason='cancelled_before_start' WHERE id=%s", (legacy_id,))
    assert timeline(legacy_id)["manifest"] is None


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_rag_search_audit_hashes_results_and_errors(monkeypatch):
    team_id = uuid4()
    query = "原始 query"
    rewritten = "rewritten query"
    chunk_ids = [uuid4(), uuid4()]
    rows = [
        {"id": item, "document_id": uuid4(), "version_id": uuid4(), "content": f"content-{index}", "source_locator": {"kind": "txt", "line_start": 1, "line_end": 1}, "title": f"title-{index}"}
        for index, item in enumerate(chunk_ids)
    ]
    monkeypatch.setattr(retrieval, "rewrite", lambda value: rewritten)
    monkeypatch.setattr(retrieval, "embed", lambda *args: [[0.1] * 1024])
    monkeypatch.setattr(retrieval, "_dense", lambda *args: rows)
    monkeypatch.setattr(retrieval, "_sparse", lambda *args: [])
    monkeypatch.setattr(retrieval, "rerank", lambda _query, _contents: [1, 0])
    response = retrieval.search(retrieval.EvidenceSearchRequest(query=query, top_k=2), team_id, "run_audit", "tool_audit")
    expected_ids = ["ev_" + str(chunk_ids[1]), "ev_" + str(chunk_ids[0])]
    assert [item["evidence_id"] for item in response["evidences"]] == expected_ids
    with connect("RAG_DATABASE_URL") as conn:
        row = conn.execute("SELECT * FROM rag.retrieval_audit WHERE retrieval_id=%s", (response["retrieval_id"],)).fetchone()
    assert row["operation"] == "search"
    assert row["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()
    assert row["rewritten_query_sha256"] == hashlib.sha256(rewritten.encode()).hexdigest()
    assert row["selected_evidence_ids"] == expected_ids
    assert query not in json.dumps(dict(row), default=str)

    error_run, error_tool = "run_" + uuid4().hex, "tool_" + uuid4().hex
    monkeypatch.setattr(retrieval, "embed", lambda *args: (_ for _ in ()).throw(ModelFailure("EMBEDDING_NOT_CONFIGURED", False)))
    with pytest.raises(ApiError):
        retrieval.search(retrieval.EvidenceSearchRequest(query="failure"), team_id, error_run, error_tool)
    with connect("RAG_DATABASE_URL") as conn:
        failed = conn.execute("SELECT operation,selected_evidence_ids,error_code FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s", (error_run, error_tool)).fetchone()
    assert failed == {"operation": "search", "selected_evidence_ids": [], "error_code": "EMBEDDING_NOT_CONFIGURED"}


@pytest.mark.skipif(not HAS_DATABASE, reason="isolated PostgreSQL not configured")
def test_rag_evidence_read_audit_success_and_not_found():
    document_id, version_id, chunk_id, team_id = uuid4(), uuid4(), uuid4(), uuid4()
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,'Audit read','public')", (document_id,))
        conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
            VALUES (%s,%s,1,%s,'/unused/audit-read.txt','text/plain','active')""", (version_id, document_id, bytes(32)))
        conn.execute("UPDATE rag.documents SET active_version_id=%s WHERE id=%s", (version_id, document_id))
        conn.execute("""INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,source_locator,search_text)
            VALUES (%s,%s,%s,1,'audited content',%s,'audited content')""", (chunk_id, document_id, version_id, json.dumps({"kind": "txt", "line_start": 1, "line_end": 1})))
    run_id, tool_id = "run_" + uuid4().hex, "tool_" + uuid4().hex
    evidence_id = "ev_" + str(chunk_id)
    assert retrieval.read_evidence(evidence_id, team_id, run_id, tool_id)["evidence_id"] == evidence_id
    missing_run, missing_tool = "run_" + uuid4().hex, "tool_" + uuid4().hex
    with pytest.raises(ApiError):
        retrieval.read_evidence("ev_" + str(uuid4()), team_id, missing_run, missing_tool)
    with connect("RAG_DATABASE_URL") as conn:
        success = conn.execute("SELECT operation,retrieval_id,query_sha256,rewritten_query_sha256,selected_evidence_ids,error_code FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s", (run_id, tool_id)).fetchone()
        missing = conn.execute("SELECT operation,selected_evidence_ids,error_code FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s", (missing_run, missing_tool)).fetchone()
    assert success == {
        "operation": "read",
        "retrieval_id": None,
        "query_sha256": None,
        "rewritten_query_sha256": None,
        "selected_evidence_ids": [evidence_id],
        "error_code": None,
    }
    assert missing == {"operation": "read", "selected_evidence_ids": [], "error_code": "EVIDENCE_NOT_FOUND"}
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("UPDATE rag.documents SET active_version_id=NULL WHERE id=%s", (document_id,))
        conn.execute("DELETE FROM rag.chunks WHERE document_id=%s", (document_id,))
        conn.execute("DELETE FROM rag.document_versions WHERE id=%s", (version_id,))
        conn.execute("DELETE FROM rag.documents WHERE id=%s", (document_id,))
