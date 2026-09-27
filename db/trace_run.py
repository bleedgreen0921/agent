"""Join one Run's safe Agent and RAG identifiers without modifying either database."""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row


UNOBSERVED = "未观测到关联记录"
NOT_RECORDED = "未记录候选过程"


def _association(tool: dict, audit: dict) -> str:
    missing = False
    for agent_field, rag_field in (("service_request_id", "request_id"), ("retrieval_id", "retrieval_id")):
        left, right = tool[agent_field], audit[rag_field]
        if left is not None and right is not None:
            if left != right:
                return "conflict"
        else:
            missing = True
    return "fields_missing" if missing else "matched"


def _tool_observation(links: list[dict]) -> str:
    if not links:
        return UNOBSERVED
    if any(link["association"] == "conflict" for link in links):
        return "conflict"
    if any(link["association"] == "fields_missing" for link in links):
        return "fields_missing"
    return "matched"


def _candidate_trace(value: dict | None, include_candidates: bool) -> dict | None:
    if value is None:
        return None
    stages = {}
    for name in ("rewrite", "dense", "fts", "fusion", "rerank"):
        source = value.get("stages", {}).get(name, {})
        stage = {"status": source.get("status", "not_executed")}
        if name != "rewrite":
            candidates = source.get("candidates", [])
            stage["candidate_count"] = len(candidates)
            if name == "rerank":
                inputs = source.get("input_evidence_ids", [])
                stage["input_count"] = len(inputs)
                if include_candidates:
                    stage["input_evidence_ids"] = inputs
            if include_candidates:
                allowed = {"dense": ("evidence_id", "rank", "distance"),
                           "fts": ("evidence_id", "rank", "score"),
                           "fusion": ("evidence_id", "rank", "rrf_score"),
                           "rerank": ("evidence_id", "rank", "score")}[name]
                stage["candidates"] = [{key: item[key] for key in allowed if key in item}
                                       for item in candidates]
        stages[name] = stage
    return {"version": value.get("version"), "top_k": value.get("top_k"), "stages": stages}


@contextmanager
def readonly_connection(env_name: str):
    with psycopg.connect(os.environ[env_name], row_factory=dict_row) as conn:
        conn.read_only = True
        yield conn


def _id(value):
    return str(value) if value is not None else None


def _time(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _manifest(row):
    if row is None:
        return None
    data = row["manifest"]
    raw = data.get("tools", {}).get("providers", []) if isinstance(data, dict) else []
    if row["schema_version"] == 1:
        providers = [item for item in raw if isinstance(item, str)]
    else:
        providers = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            providers.append({
                "factory": item.get("factory"), "name": item.get("name"), "version": item.get("version"),
                "tools": [{"name": tool.get("name"), "version": tool.get("version"),
                           "kind": tool.get("kind"), "produces_evidence": tool.get("produces_evidence")}
                          for tool in item.get("tools", []) if isinstance(tool, dict)],
            })
    return {"schema_version": row["schema_version"], "captured_at": _time(row["captured_at"]), "providers": providers}


def trace_run(run_id: UUID, include_candidates: bool = False) -> dict:
    with readonly_connection("AGENT_DATABASE_URL") as conn:
        run = conn.execute("""SELECT id,team_id,mode,status,created_at,started_at,finished_at,
            execution_deadline_at,error_code,termination_reason,model_calls_used,tool_calls_used,
            model_call_limit,tool_call_limit FROM agent.agent_runs WHERE id=%s""", (run_id,)).fetchone()
        if run is None:
            raise LookupError("Run not found")
        manifest = conn.execute("SELECT schema_version,manifest,captured_at FROM agent.run_manifests WHERE run_id=%s", (run_id,)).fetchone()
        steps = conn.execute("""SELECT id,ordinal,status,started_at,finished_at,error_code
            FROM agent.run_steps WHERE run_id=%s ORDER BY ordinal,id""", (run_id,)).fetchall()
        models = conn.execute("""SELECT id,step_id,purpose,status,started_at,finished_at,error_code,checkpointed
            FROM agent.model_calls WHERE run_id=%s ORDER BY started_at,id""", (run_id,)).fetchall()
        tools = conn.execute("""SELECT id,step_id,triggering_model_call_id,tool_name,status,started_at,finished_at,
            service_request_id,retrieval_id,evidence_ids,error_code,checkpointed
            FROM agent.tool_calls WHERE run_id=%s ORDER BY started_at,id""", (run_id,)).fetchall()
        claims = conn.execute("""SELECT c.ordinal,c.support,e.evidence_id
            FROM agent.run_results r JOIN agent.result_claims c ON c.result_id=r.id
            LEFT JOIN agent.claim_evidence e ON e.claim_id=c.id AND e.result_id=r.id
            WHERE r.run_id=%s ORDER BY c.ordinal,e.evidence_id""", (run_id,)).fetchall()

    # The RAG role owns a separate connection and transaction; no cross-schema privileges are needed.
    with readonly_connection("RAG_DATABASE_URL") as conn:
        audits = conn.execute("""SELECT id,request_id,run_id,tool_call_id,operation,retrieval_id,
            revision_id,status,degradations,created_at,query_sha256,rewritten_query_sha256,
            selected_evidence_ids,evidence_id,error_code,candidate_trace
            FROM rag.retrieval_audit WHERE run_id=%s AND team_id=%s
            ORDER BY created_at,id""", (str(run_id), run["team_id"])).fetchall()

    tools_by_id = {str(row["id"]): row for row in tools}
    audit_by_tool: dict[str, list[dict]] = {}
    audit_items = []
    for row in audits:
        tool_id = row["tool_call_id"]
        association = _association(tools_by_id[tool_id], row) if tool_id in tools_by_id else UNOBSERVED
        if tool_id in tools_by_id:
            audit_by_tool.setdefault(tool_id, []).append({"id": str(row["id"]), "association": association})
        audit_items.append({
            "id": _id(row["id"]), "tool_call_id": tool_id, "service_request_id": row["request_id"],
            "retrieval_id": row["retrieval_id"], "operation": row["operation"], "status": row["status"],
            "revision_id": _id(row["revision_id"]), "degradations": row["degradations"],
            "created_at": _time(row["created_at"]), "query_sha256": row["query_sha256"],
            "rewritten_query_sha256": row["rewritten_query_sha256"],
            "selected_evidence_ids": row["selected_evidence_ids"], "evidence_id": _id(row["evidence_id"]),
            "error_code": row["error_code"], "agent_tool_observation": association,
            "candidate_trace": _candidate_trace(row["candidate_trace"], include_candidates),
            "candidate_trace_observation": NOT_RECORDED if row["candidate_trace"] is None else "recorded",
        })
    claim_items: dict[int, dict] = {}
    for row in claims:
        item = claim_items.setdefault(row["ordinal"], {"ordinal": row["ordinal"], "support": row["support"], "evidence_ids": []})
        if row["evidence_id"] is not None:
            item["evidence_ids"].append(row["evidence_id"])
    for item in claim_items.values():
        item["evidence_links"] = [
            {"evidence_id": evidence_id, "tool_calls": [
                {"tool_call_id": str(tool["id"]), "rag_audit_ids": [link["id"] for link in audit_by_tool.get(str(tool["id"]), [])],
                 "rag_audit_links": audit_by_tool.get(str(tool["id"]), []),
                 "rag_audit_observation": _tool_observation(audit_by_tool.get(str(tool["id"]), []))}
                for tool in tools if evidence_id in (tool["evidence_ids"] or [])],
             "agent_tool_observation": "recorded" if any(evidence_id in (tool["evidence_ids"] or []) for tool in tools) else UNOBSERVED}
            for evidence_id in item["evidence_ids"]]
    return {
        "schema_version": 2,
        "run": {"id": _id(run["id"]), "mode": run["mode"], "status": run["status"],
                "created_at": _time(run["created_at"]), "started_at": _time(run["started_at"]),
                "finished_at": _time(run["finished_at"]), "execution_deadline_at": _time(run["execution_deadline_at"]),
                "error_code": run["error_code"], "termination_reason": run["termination_reason"],
                "model_calls_used": run["model_calls_used"], "tool_calls_used": run["tool_calls_used"],
                "model_call_limit": run["model_call_limit"], "tool_call_limit": run["tool_call_limit"]},
        "manifest": _manifest(manifest),
        "steps": [{"id": _id(row["id"]), "ordinal": row["ordinal"], "status": row["status"],
                   "started_at": _time(row["started_at"]), "finished_at": _time(row["finished_at"]),
                   "error_code": row["error_code"]} for row in steps],
        "model_calls": [{"id": _id(row["id"]), "step_id": _id(row["step_id"]), "purpose": row["purpose"],
                         "status": row["status"], "started_at": _time(row["started_at"]),
                         "finished_at": _time(row["finished_at"]), "error_code": row["error_code"],
                         "checkpointed": row["checkpointed"]} for row in models],
        "tool_calls": [{"id": _id(row["id"]), "step_id": _id(row["step_id"]),
                        "triggering_model_call_id": _id(row["triggering_model_call_id"]),
                        "tool_name": row["tool_name"], "status": row["status"],
                        "started_at": _time(row["started_at"]), "finished_at": _time(row["finished_at"]),
                        "service_request_id": row["service_request_id"], "retrieval_id": row["retrieval_id"],
                        "evidence_ids": row["evidence_ids"], "error_code": row["error_code"],
                        "checkpointed": row["checkpointed"],
                        "rag_audit_ids": [link["id"] for link in audit_by_tool.get(str(row["id"]), [])],
                        "rag_audit_links": audit_by_tool.get(str(row["id"]), []),
                        "rag_audit_observation": _tool_observation(audit_by_tool.get(str(row["id"]), []))}
                       for row in tools],
        "rag_audits": audit_items,
        "claims": list(claim_items.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only cross-service trace for one Run")
    parser.add_argument("run_id", type=UUID, metavar="RUN_UUID")
    parser.add_argument("--include-candidates", action="store_true", help="Include ranked candidate IDs and scores")
    args = parser.parse_args(argv)
    try:
        report = trace_run(args.run_id, include_candidates=args.include_candidates)
    except (KeyError, psycopg.Error, LookupError):
        print("trace_run: Run unavailable or database query failed", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
