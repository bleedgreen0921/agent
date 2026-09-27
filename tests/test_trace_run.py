import hashlib
import json
import os
import sys
import types
from uuid import uuid4

import psycopg
import pytest
from langchain_core.tools import tool

from agent_service.manifest import execution_manifest
from agent_service.tooling import ToolDeclaration, ToolProvider, load_providers, load_tools
from agent_service.worker import main as worker_main
from db.connection import connect
from db.trace_run import NOT_RECORDED, UNOBSERVED, main, readonly_connection, trace_run


@tool
def example_tool(value: str) -> str:
    """Return the input value."""
    return value


def _provider_module(monkeypatch, factory):
    module = types.ModuleType("test_declared_provider")
    module.tools = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module.__name__ + ":tools"


def test_provider_contract_and_manifest(monkeypatch):
    spec = _provider_module(monkeypatch, lambda: ToolProvider("example", "2", (
        ToolDeclaration(example_tool, "3", "side_effect", False),
    )))
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", spec)
    assert [tool.name for tool in load_tools()] == ["example_tool"]
    assert execution_manifest()["tools"]["providers"] == [{
        "factory": spec, "name": "example", "version": "2",
        "tools": [{"name": "example_tool", "version": "3", "kind": "side_effect", "produces_evidence": False}],
    }]


@pytest.mark.parametrize("factory", [
    lambda: [example_tool],
    lambda: ToolProvider("", "1", (ToolDeclaration(example_tool, "1", "read_only", False),)),
    lambda: ToolProvider("example", "1", (ToolDeclaration(example_tool, "", "read_only", False),)),
    lambda: ToolProvider("example", "1", (ToolDeclaration(example_tool, "1", "unknown", False),)),
    lambda: ToolProvider("example", "1", (ToolDeclaration(example_tool, "1", "read_only", None),)),
])
def test_invalid_provider_fails_at_worker_start(monkeypatch, factory):
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", _provider_module(monkeypatch, factory))
    with pytest.raises(RuntimeError):
        worker_main()


def test_duplicate_tool_names_fail(monkeypatch):
    spec = _provider_module(monkeypatch, lambda: ToolProvider("example", "1", (
        ToolDeclaration(example_tool, "1", "read_only", False),
    )))
    with pytest.raises(RuntimeError, match="unique"):
        load_providers(spec + "," + spec)


@pytest.mark.skipif(not os.environ.get("IDENTITY_ADMIN_DATABASE_URL"), reason="isolated PostgreSQL not configured")
def test_trace_links_audits_claims_and_legacy_manifest_without_sensitive_text(capsys, monkeypatch):
    run_id, team_id, model_id = uuid4(), uuid4(), uuid4()
    other_team_id = uuid4()
    linked_id, missing_id, unmatched_id, repeated_id, conflict_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    audit_id, repeated_audit_id, conflict_audit_id, stray_audit_id, result_id, claim_id = (uuid4() for _ in range(6))
    foreign_audit_id, foreign_missing_audit_id, legacy_audit_id = uuid4(), uuid4(), uuid4()
    retrieval_id = "retrieval-" + uuid4().hex
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("""INSERT INTO agent.agent_runs(id,team_id,key_id,task,mode,status,request_digest,queue_deadline_at)
            VALUES (%s,%s,'trace-key','SECRET_TASK','react','completed',%s,now())""",
            (run_id, team_id, hashlib.sha256(str(run_id).encode()).digest()))
        conn.execute("INSERT INTO agent.run_manifests(run_id,schema_version,manifest) VALUES (%s,1,%s)",
                     (run_id, psycopg.types.json.Jsonb({"tools": {"providers": ["legacy:factory"]}})))
        conn.execute("""INSERT INTO agent.model_calls(id,run_id,purpose,model,status,input_summary)
            VALUES (%s,%s,'react','test-model','succeeded','{}'::jsonb)""", (model_id, run_id))
        for tool_id, name in ((linked_id, "search_evidence"), (missing_id, "read_evidence"),
                              (repeated_id, "read_evidence"), (conflict_id, "search_evidence")):
            conn.execute("""INSERT INTO agent.tool_calls(id,run_id,triggering_model_call_id,tool_name,status,
                service_request_id,retrieval_id,evidence_ids,argument_summary)
                VALUES (%s,%s,%s,%s,'succeeded','request-1',%s,'[\"ev-1\"]'::jsonb,
                    '{\"query\":\"SECRET_QUERY\"}'::jsonb)""", (tool_id, run_id, model_id, name, retrieval_id))
        conn.execute("INSERT INTO agent.run_results(id,run_id,answer) VALUES (%s,%s,'SECRET_ANSWER')", (result_id, run_id))
        conn.execute("""INSERT INTO agent.result_claims(id,result_id,ordinal,text,support)
            VALUES (%s,%s,1,'SECRET_CLAIM','evidence')""", (claim_id, result_id))
        conn.execute("""INSERT INTO agent.evidence_snapshots(result_id,evidence_id,document_id,
            document_version_id,title,content,source_locator) VALUES (%s,'ev-1','doc','version',
            'SECRET_TITLE','SECRET_CONTENT','{}'::jsonb)""", (result_id,))
        conn.execute("INSERT INTO agent.claim_evidence(claim_id,result_id,evidence_id) VALUES (%s,%s,'ev-1')", (claim_id, result_id))
    with connect("RAG_DATABASE_URL") as conn:
        for ident, tool_id, request_id, audit_retrieval_id in ((audit_id, linked_id, "request-1", retrieval_id),
                                                               (repeated_audit_id, repeated_id, "request-1", None),
                                                               (conflict_audit_id, conflict_id, "other-request", "other-" + uuid4().hex),
                                                               (stray_audit_id, unmatched_id, "request-1", None)):
            conn.execute("""INSERT INTO rag.retrieval_audit(id,request_id,run_id,tool_call_id,team_id,
                status,operation,retrieval_id,query_sha256,selected_evidence_ids,candidate_trace)
                VALUES (%s,%s,%s,%s,%s,'ok','search',%s,'hash-1','[\"ev-1\"]'::jsonb,
                    '{"version":1,"top_k":1,"stages":{"rewrite":{"status":"success"},
                    "dense":{"status":"success","candidates":[{"evidence_id":"ev-1","rank":1,"distance":0.2}]},
                    "fts":{"status":"success","candidates":[]},
                    "fusion":{"status":"success","candidates":[{"evidence_id":"ev-1","rank":1,"rrf_score":0.01}]},
                    "rerank":{"status":"success","input_evidence_ids":["ev-1"],
                    "candidates":[{"evidence_id":"ev-1","rank":1,"score":0.8}]}}}'::jsonb)""",
                (ident, request_id, str(run_id), str(tool_id), team_id, audit_retrieval_id))
        for ident, tool_id in ((foreign_audit_id, linked_id), (foreign_missing_audit_id, missing_id)):
            conn.execute("""INSERT INTO rag.retrieval_audit(id,request_id,run_id,tool_call_id,team_id,
                status,operation,query_sha256,selected_evidence_ids)
                VALUES (%s,'foreign-request',%s,%s,%s,'ok','search','FOREIGN_QUERY_HASH',
                    '[\"FOREIGN_EVIDENCE_ID\"]'::jsonb)""",
                (ident, str(run_id), str(tool_id), other_team_id))
        conn.execute("""INSERT INTO rag.retrieval_audit(id,request_id,run_id,tool_call_id,team_id,
            status,operation,selected_evidence_ids) VALUES (%s,'legacy',%s,%s,%s,'ok','search','[]'::jsonb)""",
            (legacy_audit_id, str(run_id), str(unmatched_id), team_id))
    with readonly_connection("AGENT_DATABASE_URL") as conn:
        assert conn.read_only is True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM agent.agent_runs WHERE id=%s", (run_id,))
    with readonly_connection("RAG_DATABASE_URL") as conn:
        assert conn.read_only is True
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM rag.retrieval_audit WHERE run_id=%s", (str(run_id),))
    report = trace_run(run_id)
    assert report["schema_version"] == 2
    assert report["manifest"]["schema_version"] == 1
    assert report["manifest"]["providers"] == ["legacy:factory"]
    claim = report["claims"][0]
    assert claim["ordinal"] == 1 and claim["evidence_ids"] == ["ev-1"]
    assert {source["tool_call_id"] for source in claim["evidence_links"][0]["tool_calls"]} == {
        str(linked_id), str(missing_id), str(repeated_id), str(conflict_id)}
    calls = {item["id"]: item for item in report["tool_calls"]}
    assert calls[str(linked_id)]["rag_audit_ids"] == [str(audit_id)]
    assert calls[str(linked_id)]["rag_audit_observation"] == "matched"
    assert calls[str(missing_id)]["rag_audit_observation"] == UNOBSERVED
    assert calls[str(repeated_id)]["rag_audit_ids"] == [str(repeated_audit_id)]
    assert calls[str(repeated_id)]["rag_audit_observation"] == "fields_missing"
    assert calls[str(conflict_id)]["rag_audit_observation"] == "conflict"
    audits = {item["id"]: item for item in report["rag_audits"]}
    assert set(audits) == {str(audit_id), str(repeated_audit_id), str(conflict_audit_id), str(stray_audit_id), str(legacy_audit_id)}
    assert audits[str(stray_audit_id)]["agent_tool_observation"] == UNOBSERVED
    assert audits[str(audit_id)]["query_sha256"] == "hash-1"
    assert audits[str(audit_id)]["candidate_trace"]["stages"]["dense"]["candidate_count"] == 1
    assert "candidates" not in audits[str(audit_id)]["candidate_trace"]["stages"]["dense"]
    assert audits[str(conflict_audit_id)]["agent_tool_observation"] == "conflict"
    assert audits[str(audit_id)]["revision_id"] is None
    assert audits[str(legacy_audit_id)]["candidate_trace"] is None
    assert audits[str(legacy_audit_id)]["candidate_trace_observation"] == NOT_RECORDED
    assert main([str(run_id)]) == 0
    payload = capsys.readouterr().out
    for secret in ("SECRET_TASK", "SECRET_ANSWER", "SECRET_CLAIM", "SECRET_QUERY", "SECRET_TITLE", "SECRET_CONTENT"):
        assert secret not in payload
    assert "FOREIGN_QUERY_HASH" not in payload
    assert "FOREIGN_EVIDENCE_ID" not in payload
    assert str(foreign_audit_id) not in payload
    assert str(foreign_missing_audit_id) not in payload
    assert json.loads(payload) == report
    assert main([str(run_id), "--include-candidates"]) == 0
    expanded = json.loads(capsys.readouterr().out)
    expanded_audits = {item["id"]: item for item in expanded["rag_audits"]}
    assert expanded_audits[str(audit_id)]["candidate_trace"]["stages"]["dense"]["candidates"] == [
        {"evidence_id": "ev-1", "rank": 1, "distance": 0.2}]
    for secret in ("SECRET_TASK", "SECRET_ANSWER", "SECRET_CLAIM", "SECRET_QUERY", "SECRET_TITLE", "SECRET_CONTENT"):
        assert secret not in json.dumps(expanded)
    with monkeypatch.context() as patch:
        patch.setenv("RAG_DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/invalid?connect_timeout=1")
        assert main([str(run_id)]) == 1
    assert capsys.readouterr().out == ""


def test_trace_database_unavailable_has_no_json(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/invalid?connect_timeout=1")
    assert main([str(uuid4())]) == 1
    assert capsys.readouterr().out == ""
