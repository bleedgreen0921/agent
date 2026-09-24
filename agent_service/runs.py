import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


from agent_service.versions import GRAPH_VERSION
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, RunCreate
from db.connection import connect
from identity.security import Principal


TERMINAL = {"completed", "partial", "failed", "cancelled"}


def validate_idempotency_key(key: str | None) -> None:
    if key is not None and (not 1 <= len(key) <= 128 or any(ord(c) < 32 or ord(c) > 126 for c in key)):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid Idempotency-Key")


def request_digest(body: RunCreate) -> bytes:
    canonical = json.dumps(body.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest()


def lock_id(team_id: UUID, key: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{team_id}:{key}".encode()).digest()[:8], "big", signed=True)


def create_run(body: RunCreate, principal: Principal, key: str | None) -> tuple[dict, bool]:
    validate_idempotency_key(key)
    digest = request_digest(body)
    now = datetime.now(timezone.utc)
    queue_seconds = int(os.environ.get("AGENT_QUEUE_TIMEOUT_SECONDS", "60"))
    if queue_seconds < 1:
        raise RuntimeError("AGENT_QUEUE_TIMEOUT_SECONDS must be positive")
    run_id = uuid4()
    with connect("AGENT_DATABASE_URL") as conn:
        if key:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_id(principal.team_id, key),))
            existing = conn.execute("SELECT id,status,created_at,request_digest FROM agent.agent_runs WHERE team_id=%s AND idempotency_key=%s FOR UPDATE", (principal.team_id, key)).fetchone()
            if existing:
                if bytes(existing["request_digest"]) != digest:
                    raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key conflict")
                return {"run_id": str(existing["id"]), "status": existing["status"], "created_at": existing["created_at"]}, True
        conn.execute("""INSERT INTO agent.agent_runs(id,team_id,key_id,task,mode,status,request_digest,idempotency_key,queue_deadline_at,graph_version)
            VALUES (%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s)""", (run_id, principal.team_id, principal.key_id, body.task, body.mode, digest, key, now + timedelta(seconds=queue_seconds), GRAPH_VERSION))
    return {"run_id": str(run_id), "status": "queued", "created_at": now}, False


def get_run(run_id: UUID, team_id: UUID) -> dict:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""SELECT id,mode,status,created_at,finished_at,error_code FROM agent.agent_runs
            WHERE id=%s AND team_id=%s""", (run_id, team_id)).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Run not found")
        result = None
        if row["status"] in {"completed", "partial"}:
            result = load_result(conn, run_id)
    error = {"code": row["error_code"], "message": public_error_message(row["error_code"])} if row["error_code"] else None
    return {"run_id": str(row["id"]), "mode": row["mode"], "status": row["status"], "created_at": row["created_at"], "finished_at": row["finished_at"], "result": result, "error": error}


def load_result(conn, run_id: UUID) -> dict | None:
    result = conn.execute("SELECT id,answer,notices FROM agent.run_results WHERE run_id=%s", (run_id,)).fetchone()
    if not result:
        return None
    claims = conn.execute("SELECT id,text,support,reason FROM agent.result_claims WHERE result_id=%s ORDER BY ordinal", (result["id"],)).fetchall()
    snapshots = conn.execute("""SELECT evidence_id,document_id,document_version_id,title,content,source_locator
        FROM agent.evidence_snapshots WHERE result_id=%s ORDER BY evidence_id""", (result["id"],)).fetchall()
    evidence_by_claim = {claim["id"]: [] for claim in claims}
    relations = conn.execute("SELECT claim_id,evidence_id FROM agent.claim_evidence WHERE result_id=%s ORDER BY evidence_id", (result["id"],)).fetchall()
    for relation in relations:
        evidence_by_claim[relation["claim_id"]].append(relation["evidence_id"])
    return {
        "answer": result["answer"],
        "claims": [{"text": claim["text"], "support": claim["support"], "evidence_ids": evidence_by_claim[claim["id"]], "reason": claim["reason"]} for claim in claims],
        "citations": [dict(snapshot) for snapshot in snapshots],
        "notices": result["notices"],
    }


def public_error_message(code: str) -> str:
    return {
        "QUEUE_TIMEOUT": "Run expired before execution started",
        "RUN_TIMEOUT": "Run exceeded its execution deadline",
        "MODEL_CALL_FAILED": "The model service failed",
        "TOOL_CALL_FAILED": "A tool provider failed",
        "INVALID_PLAN": "The generated plan was invalid",
        "AUTH_FAILED": "A service authentication check failed",
        "ACCESS_DENIED": "Access was denied",
        "QUOTA_EXCEEDED": "The run call budget was exhausted",
        "INTERRUPTED_UNKNOWN": "Execution stopped with an unresolved external call",
    }.get(code, "Run failed")


def cancel_run(run_id: UUID, team_id: UUID) -> tuple[dict, int]:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT status FROM agent.agent_runs WHERE id=%s AND team_id=%s FOR UPDATE", (run_id, team_id)).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Run not found")
        if row["status"] == "queued":
            status, http_status = "cancelled", 200
            conn.execute("UPDATE agent.agent_runs SET status='cancelled',cancellation_requested_at=now(),finished_at=now(),termination_reason='cancelled_before_start' WHERE id=%s", (run_id,))
        elif row["status"] in {"running", "cancelling"}:
            status, http_status = "cancelling", 202
            conn.execute("UPDATE agent.agent_runs SET status='cancelling',cancellation_requested_at=COALESCE(cancellation_requested_at,now()) WHERE id=%s", (run_id,))
        else:
            status, http_status = row["status"], 200
    return {"run_id": str(run_id), "status": status}, http_status


def trace(run_id: UUID) -> dict:
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("""SELECT id,team_id,mode,status,created_at,started_at,finished_at,error_code,
            model_calls_used,tool_calls_used,model_call_limit,tool_call_limit,termination_reason
            FROM agent.agent_runs WHERE id=%s""", (run_id,)).fetchone()
        if not run:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Run not found")
        steps = conn.execute("SELECT * FROM agent.run_steps WHERE run_id=%s ORDER BY ordinal", (run_id,)).fetchall()
        models = conn.execute("SELECT * FROM agent.model_calls WHERE run_id=%s ORDER BY started_at,id", (run_id,)).fetchall()
        tools = conn.execute("SELECT * FROM agent.tool_calls WHERE run_id=%s ORDER BY started_at,id", (run_id,)).fetchall()
    return {"run": dict(run), "steps": [dict(row) for row in steps], "model_calls": [dict(row) for row in models], "tool_calls": [dict(row) for row in tools]}


def timeline(run_id: UUID) -> dict:
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("""SELECT id,mode,status,created_at,started_at,finished_at,error_code,termination_reason
            FROM agent.agent_runs WHERE id=%s""", (run_id,)).fetchone()
        if not run:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Run not found")
        manifest = conn.execute("SELECT schema_version,manifest,captured_at FROM agent.run_manifests WHERE run_id=%s", (run_id,)).fetchone()
        steps = conn.execute("""SELECT id,ordinal,goal,completion_condition,status,result_summary,
            started_at,finished_at,error_code FROM agent.run_steps
            WHERE run_id=%s AND started_at IS NOT NULL""", (run_id,)).fetchall()
        models = conn.execute("""SELECT id,step_id,purpose,model,status,started_at,finished_at,
            input_summary,output_summary,input_tokens,output_tokens,error_code,checkpointed
            FROM agent.model_calls WHERE run_id=%s""", (run_id,)).fetchall()
        tools = conn.execute("""SELECT id,step_id,triggering_model_call_id,tool_name,status,started_at,finished_at,
            argument_summary,result_summary,service_request_id,retrieval_id,evidence_ids,error_code,checkpointed
            FROM agent.tool_calls WHERE run_id=%s""", (run_id,)).fetchall()
        result = conn.execute("""SELECT r.id,r.created_at,
            (SELECT count(*) FROM agent.result_claims c WHERE c.result_id=r.id) AS claim_count,
            (SELECT count(*) FROM agent.evidence_snapshots e WHERE e.result_id=r.id) AS citation_count,
            jsonb_array_length(r.notices) AS notice_count
            FROM agent.run_results r WHERE r.run_id=%s""", (run_id,)).fetchone()

    run_entity = str(run["id"])
    events: list[dict] = [
        {
            "event_id": f"run:{run_entity}:created",
            "type": "run_created",
            "occurred_at": run["created_at"],
            "finished_at": None,
            "status": "queued",
            "entity_id": run_entity,
            "step_id": None,
            "parent_id": None,
            "name": None,
            "details": {"mode": run["mode"]},
        }
    ]
    if manifest:
        events.append({
            "event_id": f"run:{run_entity}:manifest",
            "type": "manifest_captured",
            "occurred_at": manifest["captured_at"],
            "finished_at": None,
            "status": "captured",
            "entity_id": run_entity,
            "step_id": None,
            "parent_id": None,
            "name": None,
            "details": {"schema_version": manifest["schema_version"]},
        })
    if run["started_at"]:
        events.append({
            "event_id": f"run:{run_entity}:started",
            "type": "run_started",
            "occurred_at": run["started_at"],
            "finished_at": None,
            "status": "running",
            "entity_id": run_entity,
            "step_id": None,
            "parent_id": None,
            "name": None,
            "details": {},
        })
    for row in steps:
        entity_id = str(row["id"])
        events.append({
            "event_id": f"step:{entity_id}",
            "type": "step",
            "occurred_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "entity_id": entity_id,
            "step_id": entity_id,
            "parent_id": None,
            "name": None,
            "details": {
                "ordinal": row["ordinal"],
                "goal": row["goal"],
                "completion_condition": row["completion_condition"],
                "result_summary": row["result_summary"],
                "error_code": row["error_code"],
            },
        })
    for row in models:
        entity_id = str(row["id"])
        events.append({
            "event_id": f"model:{entity_id}",
            "type": "model_call",
            "occurred_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "entity_id": entity_id,
            "step_id": str(row["step_id"]) if row["step_id"] else None,
            "parent_id": None,
            "name": row["model"],
            "details": {
                "purpose": row["purpose"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "input_summary": row["input_summary"],
                "output_summary": row["output_summary"],
                "error_code": row["error_code"],
                "checkpointed": row["checkpointed"],
            },
        })
    for row in tools:
        entity_id = str(row["id"])
        events.append({
            "event_id": f"tool:{entity_id}",
            "type": "tool_call",
            "occurred_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "entity_id": entity_id,
            "step_id": str(row["step_id"]) if row["step_id"] else None,
            "parent_id": str(row["triggering_model_call_id"]) if row["triggering_model_call_id"] else None,
            "name": row["tool_name"],
            "details": {
                "tool_name": row["tool_name"],
                "argument_summary": row["argument_summary"],
                "result_summary": row["result_summary"],
                "service_request_id": row["service_request_id"],
                "retrieval_id": row["retrieval_id"],
                "evidence_ids": row["evidence_ids"],
                "error_code": row["error_code"],
                "checkpointed": row["checkpointed"],
            },
        })
    if result:
        entity_id = str(result["id"])
        events.append({
            "event_id": f"result:{entity_id}",
            "type": "result_published",
            "occurred_at": result["created_at"],
            "finished_at": None,
            "status": "published",
            "entity_id": entity_id,
            "step_id": None,
            "parent_id": None,
            "name": None,
            "details": {
                "claim_count": result["claim_count"],
                "citation_count": result["citation_count"],
                "notice_count": result["notice_count"],
            },
        })
    if run["finished_at"]:
        events.append({
            "event_id": f"run:{run_entity}:finished",
            "type": "run_finished",
            "occurred_at": run["finished_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "entity_id": run_entity,
            "step_id": None,
            "parent_id": None,
            "name": None,
            "details": {"termination_reason": run["termination_reason"], "error_code": run["error_code"]},
        })

    priority = {
        "run_created": 0,
        "manifest_captured": 1,
        "run_started": 2,
        "step": 3,
        "model_call": 4,
        "tool_call": 5,
        "result_published": 6,
        "run_finished": 7,
    }
    events.sort(key=lambda item: (item["occurred_at"], priority[item["type"]], item["entity_id"] or ""))
    for sequence, event in enumerate(events, 1):
        event["sequence"] = sequence
    return {
        "run_id": run_entity,
        "status": run["status"],
        "manifest": {
            "schema_version": manifest["schema_version"],
            "captured_at": manifest["captured_at"],
            "data": manifest["manifest"],
        } if manifest else None,
        "events": events,
    }
