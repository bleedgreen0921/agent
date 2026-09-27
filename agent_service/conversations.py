"""User-owned conversations and immutable turn submission."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import hashlib
import json
import os

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import Field

from agent_service.runs import cancel_run, get_run, validate_idempotency_key
from agent_service.versions import GRAPH_VERSION
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, RunCreate, StrictModel
from db.connection import connect
from identity.security import Principal, agent_user


router = APIRouter(prefix="/v1/conversations", tags=["conversations"])
memory_router = APIRouter(prefix="/v1/memories", tags=["personal memory"])


class TurnCreate(RunCreate):
    pass


def owned(conn, conversation_id: UUID, principal: Principal, lock: bool = False):
    row = conn.execute("""SELECT id,created_at FROM agent.conversations
        WHERE id=%s AND team_id=%s AND user_id=%s""" + (" FOR UPDATE" if lock else ""),
        (conversation_id, principal.team_id, principal.user_id)).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Conversation not found")
    return row


@router.post("", status_code=201)
def create_conversation(principal: Principal = Depends(agent_user)):
    conversation_id = uuid4()
    with connect("AGENT_DATABASE_URL") as conn:
        row = conn.execute("""INSERT INTO agent.conversations(id,team_id,user_id)
            VALUES (%s,%s,%s) RETURNING created_at""", (conversation_id, principal.team_id, principal.user_id)).fetchone()
    return {"conversation_id": str(conversation_id), "created_at": row["created_at"]}


@router.get("")
def list_conversations(limit: int = Query(30, ge=1, le=100), principal: Principal = Depends(agent_user)):
    with connect("AGENT_DATABASE_URL") as conn:
        rows = conn.execute("""SELECT c.id,c.created_at,count(t.id) AS turn_count
            FROM agent.conversations c LEFT JOIN agent.conversation_turns t ON t.conversation_id=c.id
            WHERE c.team_id=%s AND c.user_id=%s GROUP BY c.id
            ORDER BY c.created_at DESC,c.id DESC LIMIT %s""", (principal.team_id, principal.user_id, limit)).fetchall()
    return {"items": [{"conversation_id": str(r["id"]), "created_at": r["created_at"], "turn_count": r["turn_count"]} for r in rows]}


@router.get("/{conversation_id}")
def read_conversation(conversation_id: UUID, principal: Principal = Depends(agent_user)):
    with connect("AGENT_DATABASE_URL") as conn:
        conversation = owned(conn, conversation_id, principal)
        rows = conn.execute("""SELECT t.id,t.ordinal,t.run_id,t.user_text,t.created_at,r.status,r.finished_at,
            rr.answer FROM agent.conversation_turns t JOIN agent.agent_runs r ON r.id=t.run_id
            LEFT JOIN agent.run_results rr ON rr.run_id=r.id
            WHERE t.conversation_id=%s ORDER BY t.ordinal""", (conversation_id,)).fetchall()
    return {"conversation_id": str(conversation_id), "created_at": conversation["created_at"],
            "turns": [{"turn_id": str(r["id"]), "ordinal": r["ordinal"], "run_id": str(r["run_id"]),
                       "user_text": r["user_text"], "status": r["status"], "answer": r["answer"],
                       "created_at": r["created_at"], "finished_at": r["finished_at"]} for r in rows]}


@router.post("/{conversation_id}/turns")
def submit_turn(conversation_id: UUID, body: TurnCreate, response: Response,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                principal: Principal = Depends(agent_user)):
    validate_idempotency_key(idempotency_key)
    if not idempotency_key:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Idempotency-Key is required")
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).digest()
    now = datetime.now(timezone.utc)
    queue_seconds = int(os.environ.get("AGENT_QUEUE_TIMEOUT_SECONDS", "60"))
    if queue_seconds < 1:
        raise RuntimeError("AGENT_QUEUE_TIMEOUT_SECONDS must be positive")
    with connect("AGENT_DATABASE_URL") as conn:
        owned(conn, conversation_id, principal, lock=True)
        prior = conn.execute("""SELECT t.id,t.run_id,t.ordinal,t.request_digest,r.status,t.created_at
            FROM agent.conversation_turns t JOIN agent.agent_runs r ON r.id=t.run_id
            WHERE t.conversation_id=%s AND t.idempotency_key=%s""", (conversation_id, idempotency_key)).fetchone()
        if prior:
            if bytes(prior["request_digest"]) != digest:
                raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key conflict")
            response.status_code = 200
            return {"turn_id": str(prior["id"]), "ordinal": prior["ordinal"], "run_id": str(prior["run_id"]),
                    "status": prior["status"], "created_at": prior["created_at"]}
        active = conn.execute("""SELECT 1 FROM agent.conversation_turns t JOIN agent.agent_runs r ON r.id=t.run_id
            WHERE t.conversation_id=%s AND r.status IN ('queued','running','cancelling') LIMIT 1""", (conversation_id,)).fetchone()
        if active:
            raise ApiError(409, ErrorCode.CONVERSATION_BUSY, "Conversation has an active turn")
        ordinal = conn.execute("SELECT COALESCE(max(ordinal),0)+1 AS next FROM agent.conversation_turns WHERE conversation_id=%s", (conversation_id,)).fetchone()["next"]
        run_id, turn_id = uuid4(), uuid4()
        conn.execute("""INSERT INTO agent.agent_runs(id,team_id,key_id,task,mode,status,request_digest,queue_deadline_at,graph_version)
            VALUES (%s,%s,%s,%s,%s,'queued',%s,%s,%s)""",
            (run_id, principal.team_id, principal.key_id, body.task, body.mode, digest, now + timedelta(seconds=queue_seconds), GRAPH_VERSION))
        row = conn.execute("""INSERT INTO agent.conversation_turns(id,conversation_id,ordinal,run_id,user_text,idempotency_key,request_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING created_at""",
            (turn_id, conversation_id, ordinal, run_id, body.task, idempotency_key, digest)).fetchone()
        conn.execute("INSERT INTO agent.memory_jobs(id,kind,turn_id) VALUES (%s,'extract',%s)", (uuid4(), turn_id))
    response.status_code = 202
    return {"turn_id": str(turn_id), "ordinal": ordinal, "run_id": str(run_id), "status": "queued", "created_at": row["created_at"]}


def owned_turn(conn, conversation_id: UUID, turn_id: UUID, principal: Principal):
    owned(conn, conversation_id, principal)
    row = conn.execute("SELECT run_id,ordinal,user_text,created_at FROM agent.conversation_turns WHERE id=%s AND conversation_id=%s", (turn_id, conversation_id)).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Turn not found")
    return row


@router.get("/{conversation_id}/turns/{turn_id}")
def read_turn(conversation_id: UUID, turn_id: UUID, principal: Principal = Depends(agent_user)):
    with connect("AGENT_DATABASE_URL") as conn:
        row = owned_turn(conn, conversation_id, turn_id, principal)
    return {"turn_id": str(turn_id), "ordinal": row["ordinal"], "user_text": row["user_text"],
            "created_at": row["created_at"], **get_run(row["run_id"], principal.team_id)}


@router.post("/{conversation_id}/turns/{turn_id}/cancel")
def cancel_turn(conversation_id: UUID, turn_id: UUID, response: Response, principal: Principal = Depends(agent_user)):
    with connect("AGENT_DATABASE_URL") as conn:
        row = owned_turn(conn, conversation_id, turn_id, principal)
    payload, status = cancel_run(row["run_id"], principal.team_id)
    response.status_code = status
    return payload


@memory_router.get("")
def list_memories(limit: int = Query(100, ge=1, le=200), principal: Principal = Depends(agent_user)):
    with connect("AGENT_DATABASE_URL") as conn:
        rows = conn.execute("""SELECT f.id,f.statement,f.source_quote,f.subject,f.subject_quote,f.source_turn_id,
            f.subject_turn_id,f.stated_at,t.conversation_id,t.ordinal
            FROM agent.personal_facts f JOIN agent.conversation_turns t ON t.id=f.source_turn_id
            WHERE f.team_id=%s AND f.user_id=%s ORDER BY f.stated_at DESC,f.id DESC LIMIT %s""",
            (principal.team_id, principal.user_id, limit)).fetchall()
    return {"items": [{**dict(r), "id": str(r["id"]), "source_turn_id": str(r["source_turn_id"]),
                       "subject_turn_id": str(r["subject_turn_id"]), "conversation_id": str(r["conversation_id"])} for r in rows]}
