import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_service.manifest import execution_manifest, schema_version
from db.connection import connect


def claim_run() -> dict | None:
    lease_seconds = int(os.environ.get("AGENT_LEASE_SECONDS", "120"))
    execution_seconds = int(os.environ.get("AGENT_EXECUTION_TIMEOUT_SECONDS", "300"))
    concurrency = int(os.environ.get("AGENT_MAX_CONCURRENT_RUNS", "2"))
    with connect("AGENT_DATABASE_URL") as conn:
        conn.execute("SELECT pg_advisory_xact_lock(824731091)")
        conn.execute("""UPDATE agent.agent_runs SET status='failed',error_code='QUEUE_TIMEOUT',
            termination_reason='queue_timeout',finished_at=now()
            WHERE status='queued' AND queue_deadline_at<=now()""")
        cancelled = conn.execute("""UPDATE agent.agent_runs SET status='cancelled',error_code=NULL,
            termination_reason='cancelled',finished_at=now(),lease_token=NULL,leased_until=NULL
            WHERE status='cancelling' AND (leased_until IS NULL OR leased_until<=now() OR execution_deadline_at<=now())
            RETURNING id""").fetchall()
        for item in cancelled:
            conn.execute("UPDATE agent.model_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (item["id"],))
            conn.execute("UPDATE agent.tool_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (item["id"],))
        expired = conn.execute("""UPDATE agent.agent_runs SET status='failed',error_code='RUN_TIMEOUT',
            termination_reason='run_timeout',finished_at=now(),lease_token=NULL,leased_until=NULL
            WHERE status='running' AND execution_deadline_at<=now() RETURNING id""").fetchall()
        for item in expired:
            conn.execute("UPDATE agent.model_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (item["id"],))
            conn.execute("UPDATE agent.tool_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (item["id"],))
        running = conn.execute("SELECT count(*) AS n FROM agent.agent_runs WHERE status IN ('running','cancelling') AND leased_until>now()").fetchone()["n"]
        if running >= concurrency:
            return None
        row = conn.execute("""SELECT id,status,started_at,execution_deadline_at FROM agent.agent_runs
            WHERE (status='queued' AND queue_deadline_at>now())
               OR (status='running' AND leased_until<=now() AND execution_deadline_at>now())
            ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,created_at
            FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:
            return None
        if row["status"] == "running" and not safe_to_resume(conn, row["id"]):
            mark_interrupted(conn, row["id"])
            return None
        token = uuid4()
        first = row["started_at"] is None
        conn.execute(
            """INSERT INTO agent.run_manifests(run_id,schema_version,manifest)
            VALUES (%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
            (row["id"], schema_version(), Jsonb(execution_manifest())),
        )
        conn.execute("""UPDATE agent.agent_runs SET status='running',lease_token=%s,
            leased_until=now()+(%s * interval '1 second'),heartbeat_at=now(),
            started_at=COALESCE(started_at,now()),
            execution_deadline_at=COALESCE(execution_deadline_at,now()+(%s * interval '1 second'))
            WHERE id=%s""", (token, lease_seconds, execution_seconds, row["id"]))
        return {"run_id": row["id"], "lease_token": token, "first_claim": first}


def safe_to_resume(conn, run_id: UUID) -> bool:
    unsafe = conn.execute("""SELECT 1 FROM agent.model_calls WHERE run_id=%s AND (status='started' OR NOT checkpointed)
        UNION ALL SELECT 1 FROM agent.tool_calls WHERE run_id=%s AND (status='started' OR NOT checkpointed) LIMIT 1""", (run_id, run_id)).fetchone()
    return not bool(unsafe)


def mark_checkpointed_calls(run_id: UUID, token: UUID) -> bool:
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("""SELECT id FROM agent.agent_runs WHERE id=%s AND lease_token=%s
            AND status='running' AND leased_until>now() AND execution_deadline_at>now() FOR UPDATE""", (run_id, token)).fetchone()
        if not run:
            return False
        conn.execute("UPDATE agent.model_calls SET checkpointed=true WHERE run_id=%s AND status<>'started' AND NOT checkpointed", (run_id,))
        conn.execute("UPDATE agent.tool_calls SET checkpointed=true WHERE run_id=%s AND status<>'started' AND NOT checkpointed", (run_id,))
    return True


def mark_interrupted(conn, run_id: UUID) -> None:
    conn.execute("UPDATE agent.model_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
    conn.execute("UPDATE agent.tool_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
    conn.execute("""UPDATE agent.agent_runs SET status='failed',error_code='INTERRUPTED_UNKNOWN',
        termination_reason='interrupted_unknown',finished_at=now(),lease_token=NULL,leased_until=NULL WHERE id=%s""", (run_id,))


def heartbeat(run_id: UUID, token: UUID) -> bool:
    lease_seconds = int(os.environ.get("AGENT_LEASE_SECONDS", "120"))
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""UPDATE agent.agent_runs SET leased_until=now()+(%s * interval '1 second'),heartbeat_at=now()
            WHERE id=%s AND lease_token=%s AND status='running' AND execution_deadline_at>now()
            RETURNING id""", (lease_seconds, run_id, token)).fetchone()
    return bool(row)


def control_state(run_id: UUID, token: UUID) -> str:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT status,execution_deadline_at,lease_token FROM agent.agent_runs WHERE id=%s", (run_id,)).fetchone()
    if not row or row["lease_token"] != token:
        return "lost"
    if row["status"] == "cancelling":
        return "cancel"
    if row["execution_deadline_at"] <= datetime.now(timezone.utc):
        return "timeout"
    return "running"


def revoke_and_finish(run_id: UUID, token: UUID, reason: str) -> None:
    status, error = ("cancelled", None) if reason == "cancel" else ("failed", "RUN_TIMEOUT")
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT id FROM agent.agent_runs WHERE id=%s AND lease_token=%s FOR UPDATE", (run_id, token)).fetchone()
        if not row:
            return
        conn.execute("UPDATE agent.model_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
        conn.execute("UPDATE agent.tool_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
        conn.execute("""UPDATE agent.agent_runs SET status=%s,error_code=%s,termination_reason=%s,
            finished_at=now(),lease_token=NULL,leased_until=NULL WHERE id=%s AND lease_token=%s""", (status, error, "cancelled" if reason == "cancel" else "run_timeout", run_id, token))


def child_exited(run_id: UUID, token: UUID) -> None:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("SELECT status FROM agent.agent_runs WHERE id=%s AND lease_token=%s FOR UPDATE", (run_id, token)).fetchone()
        if not row or row["status"] in {"completed", "partial", "failed", "cancelled"}:
            return
        if row["status"] == "cancelling":
            conn.execute("UPDATE agent.model_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
            conn.execute("UPDATE agent.tool_calls SET status='interrupted_unknown',finished_at=now(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))
            conn.execute("UPDATE agent.agent_runs SET status='cancelled',termination_reason='cancelled',finished_at=now(),lease_token=NULL,leased_until=NULL WHERE id=%s", (run_id,))
        elif not safe_to_resume(conn, run_id):
            mark_interrupted(conn, run_id)
        else:
            conn.execute("UPDATE agent.agent_runs SET leased_until=now(),lease_token=NULL WHERE id=%s", (run_id,))


def reserve_model(run_id: UUID, token: UUID, purpose: str, model: str, step_id: UUID | None = None, final: bool = False, input_summary: dict | None = None) -> UUID:
    return _reserve(run_id, token, "model", purpose, model, step_id, final, input_summary=input_summary)


def reserve_tool(run_id: UUID, token: UUID, tool_name: str, arguments: dict, step_id: UUID | None = None, triggering_model_call_id: UUID | None = None) -> UUID:
    return _reserve(run_id, token, "tool", tool_name, "", step_id, False, arguments=arguments, triggering_model_call_id=triggering_model_call_id)


def _reserve(run_id: UUID, token: UUID, kind: str, purpose: str, model: str, step_id: UUID | None, final: bool, *, input_summary: dict | None = None, arguments: dict | None = None, triggering_model_call_id: UUID | None = None) -> UUID:
    call_id = uuid4()
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("""SELECT model_calls_used,tool_calls_used,model_call_limit,tool_call_limit
            FROM agent.agent_runs WHERE id=%s AND lease_token=%s AND status='running'
            AND leased_until>now() AND execution_deadline_at>now() FOR UPDATE""", (run_id, token)).fetchone()
        if not run:
            raise PermissionError("execution lease is not active")
        used = run[f"{kind}_calls_used"]
        limit = run[f"{kind}_call_limit"]
        allowed = used < limit if kind == "tool" or final else used < limit - 1
        if not allowed:
            raise BudgetExhausted(kind)
        conn.execute(f"UPDATE agent.agent_runs SET {kind}_calls_used={kind}_calls_used+1 WHERE id=%s", (run_id,))
        if kind == "model":
            conn.execute("""INSERT INTO agent.model_calls(id,run_id,step_id,purpose,model,status,input_summary)
                VALUES (%s,%s,%s,%s,%s,'started',%s)""", (call_id, run_id, step_id, purpose, model, Jsonb(input_summary or {})))
        else:
            conn.execute("""INSERT INTO agent.tool_calls(id,run_id,step_id,triggering_model_call_id,tool_name,status,argument_summary)
                VALUES (%s,%s,%s,%s,%s,'started',%s)""", (call_id, run_id, step_id, triggering_model_call_id, purpose, Jsonb(arguments or {})))
    return call_id


class BudgetExhausted(RuntimeError):
    pass


def settle_model(run_id: UUID, token: UUID, call_id: UUID, status: str, *, input_tokens: int | None = None, output_tokens: int | None = None, output_summary: dict | None = None, error_code: str | None = None) -> bool:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""UPDATE agent.model_calls c SET status=%s,finished_at=now(),input_tokens=%s,
            output_tokens=%s,output_summary=%s,error_code=%s
            FROM agent.agent_runs r WHERE c.id=%s AND c.run_id=%s AND c.status='started'
              AND r.id=c.run_id AND r.lease_token=%s AND r.status='running'
            RETURNING c.id""", (status, input_tokens, output_tokens, Jsonb(output_summary) if output_summary is not None else None, error_code, call_id, run_id, token)).fetchone()
    return bool(row)


def settle_tool(run_id: UUID, token: UUID, call_id: UUID, status: str, *, result_summary: dict | None = None, service_request_id: str | None = None, retrieval_id: str | None = None, evidence_ids: list[str] | None = None, error_code: str | None = None) -> bool:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""UPDATE agent.tool_calls c SET status=%s,finished_at=now(),result_summary=%s,
            service_request_id=%s,retrieval_id=%s,evidence_ids=%s,error_code=%s
            FROM agent.agent_runs r WHERE c.id=%s AND c.run_id=%s AND c.status='started'
              AND r.id=c.run_id AND r.lease_token=%s AND r.status='running'
            RETURNING c.id""", (status, Jsonb(result_summary) if result_summary is not None else None, service_request_id, retrieval_id, Jsonb(evidence_ids or []), error_code, call_id, run_id, token)).fetchone()
    return bool(row)


def fail_run(run_id: UUID, token: UUID, code: str, reason: str | None = None) -> bool:
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""UPDATE agent.agent_runs SET status='failed',error_code=%s,
            termination_reason=%s,finished_at=now(),lease_token=NULL,leased_until=NULL
            WHERE id=%s AND lease_token=%s AND status='running' RETURNING id""", (code, reason or code.lower(), run_id, token)).fetchone()
    return bool(row)


def publish_result(run_id: UUID, token: UUID, draft: dict, evidence: dict[str, dict], partial: bool, termination_reason: str) -> bool:
    cited_ids = []
    for claim in draft["claims"]:
        if claim["support"] == "evidence" and not claim["evidence_ids"]:
            raise ValueError("supported claim has no evidence")
        if claim["support"] == "unverified" and not claim.get("reason"):
            raise ValueError("unverified claim requires a reason")
        for evidence_id in claim["evidence_ids"]:
            if evidence_id not in evidence:
                raise ValueError("citation was not received by this run")
            cited_ids.append(evidence_id)
    cited_ids = list(dict.fromkeys(cited_ids))
    declared = [item["evidence_id"] for item in draft["citations"]]
    if set(declared) != set(cited_ids) or len(declared) != len(set(declared)):
        raise ValueError("citation list does not match claim evidence")
    result_id = uuid4()
    with connect("AGENT_DATABASE_URL") as conn:
        run = conn.execute("SELECT id FROM agent.agent_runs WHERE id=%s AND lease_token=%s AND status='running' FOR UPDATE", (run_id, token)).fetchone()
        if not run:
            return False
        conn.execute("INSERT INTO agent.run_results(id,run_id,answer,notices) VALUES (%s,%s,%s,%s)", (result_id, run_id, draft["answer"], Jsonb(draft.get("notices", []))))
        for evidence_id in cited_ids:
            item = evidence[evidence_id]
            conn.execute("""INSERT INTO agent.evidence_snapshots(result_id,evidence_id,document_id,document_version_id,title,content,source_locator)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (result_id, evidence_id, item["document_id"], item["document_version_id"], item["title"], item["content"], Jsonb(item["source_locator"])))
        for ordinal, claim in enumerate(draft["claims"], 1):
            claim_id = uuid4()
            conn.execute("""INSERT INTO agent.result_claims(id,result_id,ordinal,text,support,reason)
                VALUES (%s,%s,%s,%s,%s,%s)""", (claim_id, result_id, ordinal, claim["text"], claim["support"], claim.get("reason")))
            for evidence_id in claim["evidence_ids"]:
                conn.execute("INSERT INTO agent.claim_evidence(claim_id,result_id,evidence_id) VALUES (%s,%s,%s)", (claim_id, result_id, evidence_id))
        conn.execute("""UPDATE agent.agent_runs SET status=%s,termination_reason=%s,finished_at=now(),
            lease_token=NULL,leased_until=NULL WHERE id=%s""", ("partial" if partial else "completed", termination_reason, run_id))
    return True
