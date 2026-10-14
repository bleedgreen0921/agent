"""Personal conversations and immutable turn submission."""
from uuid import UUID, uuid4
import hashlib
import json
import os
from fastapi import APIRouter, Header, Query, Response
from agent_service.runs import cancel_run, get_run, validate_idempotency_key
from agent_service.versions import GRAPH_VERSION
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, RunCreate
from execution.persistence import transaction as connect
from execution import store
from execution.contracts import TaskKind, TaskRef
from execution.inputs import enqueue_memory
from execution.retry import retry_transaction
router = APIRouter(prefix='/v1/conversations', tags=['conversations'])
memory_router = APIRouter(prefix='/v1/memories', tags=['personal memory'])

class TurnCreate(RunCreate):
    pass

async def existing(conn, conversation_id: UUID, lock: bool=False):
    row = await (await conn.execute('SELECT id,created_at FROM agent.conversations\n        WHERE id=%s' + (' FOR UPDATE' if lock else ''), (conversation_id,))).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, 'Conversation not found')
    return row

@router.post('', status_code=201)
@retry_transaction
async def create_conversation():
    conversation_id = uuid4()
    async with connect('AGENT_DATABASE_URL') as conn:
        row = await (await conn.execute('INSERT INTO agent.conversations(id)\n            VALUES (%s) RETURNING created_at', (conversation_id,))).fetchone()
    return {'conversation_id': str(conversation_id), 'created_at': row['created_at']}

@router.get('')
async def list_conversations(limit: int=Query(30, ge=1, le=100)):
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        rows = await (await conn.execute('SELECT c.id,c.created_at,count(t.id) AS turn_count\n            FROM agent.conversations c LEFT JOIN agent.conversation_turns t ON t.conversation_id=c.id\n            GROUP BY c.id\n            ORDER BY c.created_at DESC,c.id DESC LIMIT %s', (limit,))).fetchall()
    return {'items': [{'conversation_id': str(r['id']), 'created_at': r['created_at'], 'turn_count': r['turn_count']} for r in rows]}

@router.get('/{conversation_id}')
async def read_conversation(conversation_id: UUID):
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        conversation = await existing(conn, conversation_id)
        rows = await (await conn.execute('SELECT t.id,t.ordinal,t.run_id,t.user_text,t.created_at,r.status,r.finished_at,\n            rr.answer FROM agent.conversation_turns t JOIN agent.run_state r ON r.id=t.run_id\n            LEFT JOIN agent.run_results rr ON rr.run_id=r.id\n            WHERE t.conversation_id=%s ORDER BY t.ordinal', (conversation_id,))).fetchall()
    return {'conversation_id': str(conversation_id), 'created_at': conversation['created_at'], 'turns': [{'turn_id': str(r['id']), 'ordinal': r['ordinal'], 'run_id': str(r['run_id']), 'user_text': r['user_text'], 'status': r['status'], 'answer': r['answer'], 'created_at': r['created_at'], 'finished_at': r['finished_at']} for r in rows]}

@router.post('/{conversation_id}/turns')
@retry_transaction
async def submit_turn(conversation_id: UUID, body: TurnCreate, response: Response, idempotency_key: str | None=Header(default=None, alias='Idempotency-Key')):
    validate_idempotency_key(idempotency_key)
    if not idempotency_key:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Idempotency-Key is required')
    from agent_service.storage.runs import request_digest
    digest = request_digest(body)
    queue_seconds = int(os.environ.get('AGENT_QUEUE_TIMEOUT_SECONDS', '60'))
    if queue_seconds < 1:
        raise RuntimeError('AGENT_QUEUE_TIMEOUT_SECONDS must be positive')
    async with connect('AGENT_DATABASE_URL') as conn:
        await existing(conn, conversation_id, lock=True)
        prior = await (await conn.execute('SELECT t.id,t.run_id,t.ordinal,t.request_digest,r.status,t.created_at\n            FROM agent.conversation_turns t JOIN agent.run_state r ON r.id=t.run_id\n            WHERE t.conversation_id=%s AND t.idempotency_key=%s', (conversation_id, idempotency_key))).fetchone()
        if prior:
            if bytes(prior['request_digest']) != digest:
                raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, 'Idempotency key conflict')
            response.status_code = 200
            return {'turn_id': str(prior['id']), 'ordinal': prior['ordinal'], 'run_id': str(prior['run_id']), 'status': prior['status'], 'created_at': prior['created_at']}
        from agent_service.intent import validate_grants
        try: validate_grants(body.tool_authorizations)
        except ValueError as exc: raise ApiError(422, ErrorCode.INVALID_REQUEST, str(exc)) from exc
        active = await (await conn.execute("SELECT 1 FROM agent.conversation_turns t JOIN agent.run_state r ON r.id=t.run_id\n            WHERE t.conversation_id=%s AND r.status IN ('queued','running','cancelling') LIMIT 1", (conversation_id,))).fetchone()
        if active:
            raise ApiError(409, ErrorCode.CONVERSATION_BUSY, 'Conversation has an active turn')
        ordinal = (await (await conn.execute('SELECT COALESCE(max(ordinal),0)+1 AS next FROM agent.conversation_turns WHERE conversation_id=%s', (conversation_id,))).fetchone())['next']
        run_id, turn_id = (uuid4(), uuid4())
        await store.create(conn, TaskRef(run_id, TaskKind.AGENT), queue_seconds=queue_seconds)
        await conn.execute('INSERT INTO agent.agent_runs(id,task,mode,request_digest,graph_version)\n            VALUES (%s,%s,%s,%s,%s)', (run_id, body.task, body.mode, digest, GRAPH_VERSION))
        row = await (await conn.execute('INSERT INTO agent.conversation_turns(id,conversation_id,ordinal,run_id,user_text,idempotency_key,request_digest)\n            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING created_at', (turn_id, conversation_id, ordinal, run_id, body.task, idempotency_key, digest))).fetchone()
        from agent_service.storage.acceptance import insert_grants
        await insert_grants(conn,run_id,body.tool_authorizations)
        from execution.admission import reserve_memory
        await reserve_memory(conn, run_id)
        await enqueue_memory(conn, turn_id, 'extract')
    response.status_code = 202
    return {'turn_id': str(turn_id), 'ordinal': ordinal, 'run_id': str(run_id), 'status': 'queued', 'created_at': row['created_at']}

async def existing_turn(conn, conversation_id: UUID, turn_id: UUID):
    await existing(conn, conversation_id)
    row = await (await conn.execute('SELECT run_id,ordinal,user_text,created_at FROM agent.conversation_turns WHERE id=%s AND conversation_id=%s', (turn_id, conversation_id))).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, 'Turn not found')
    return row

@router.get('/{conversation_id}/turns/{turn_id}')
async def read_turn(conversation_id: UUID, turn_id: UUID):
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        row = await existing_turn(conn, conversation_id, turn_id)
    return {'turn_id': str(turn_id), 'ordinal': row['ordinal'], 'user_text': row['user_text'], 'created_at': row['created_at'], **await get_run(row['run_id'])}

@router.post('/{conversation_id}/turns/{turn_id}/cancel')
@retry_transaction(profile='control')
async def cancel_turn(conversation_id: UUID, turn_id: UUID, response: Response):
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        row = await existing_turn(conn, conversation_id, turn_id)
    payload, status = await cancel_run(row['run_id'])
    response.status_code = status
    return payload

@memory_router.get('')
async def list_memories(limit: int=Query(100, ge=1, le=200)):
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        rows = await (await conn.execute('SELECT f.id,f.statement,f.source_quote,f.subject,f.subject_quote,f.source_turn_id,\n            f.subject_turn_id,f.stated_at,t.conversation_id,t.ordinal\n            FROM agent.personal_facts f JOIN agent.conversation_turns t ON t.id=f.source_turn_id\n            ORDER BY f.stated_at DESC,f.id DESC LIMIT %s', (limit,))).fetchall()
    return {'items': [{**dict(r), 'id': str(r['id']), 'source_turn_id': str(r['source_turn_id']), 'subject_turn_id': str(r['subject_turn_id']), 'conversation_id': str(r['conversation_id'])} for r in rows]}
