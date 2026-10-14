"""Signed finite allocations; services never read another business schema."""
import base64
import hashlib
import hmac
import json
import os
from contextvars import ContextVar
from uuid import UUID, uuid5, NAMESPACE_URL
from execution import tokens
from execution.persistence import transaction
from execution.scope import current_attempt
from execution.retry import retry_transaction

dispatch_allocation=ContextVar('rag_dispatch_allocation',default=None)


def encode(value):
    payload=tokens.canonical(value).encode()
    signature=hmac.new(os.environ['RAG_SERVICE_TOKEN'].encode(),b'token-budget-v1\0'+payload,hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode()+'.'+signature


def decode(ticket,run_id,tool_id,request):
    import time
    try:
        payload,signature=ticket.split('.')
        raw=base64.b64decode(payload,altchars=b'-_',validate=True)
        expected=hmac.new(os.environ['RAG_SERVICE_TOKEN'].encode(),b'token-budget-v1\0'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):raise ValueError
        value=json.loads(raw)
        if value['run_id']!=run_id or value['tool_id']!=tool_id or value['request_sha256']!=hashlib.sha256(tokens.canonical(request).encode()).hexdigest():raise ValueError
        if value['expires_at']<=time.time() or value['limit']<=0:raise ValueError
        if value['policy_sha256']!=hashlib.sha256(tokens.canonical(tokens.settings()['models']).encode()).hexdigest():raise ValueError
        UUID(value['id'])
        return value
    except (ValueError,KeyError,TypeError):
        from contracts.errors import ApiError
        from contracts.v1 import ErrorCode
        raise ApiError(409,ErrorCode.INVALID_REQUEST,'Invalid or expired token allocation') from None


async def issue(run_id,tool_id,request):
    attempt=current_attempt()
    if attempt is None:return None,None
    from execution import store
    row=await store.check(attempt.task,attempt.token)
    identity=uuid5(NAMESPACE_URL,f'rag-budget:{run_id}:{tool_id}')
    limit=tokens.settings()['limits']['search']
    value={'id':str(identity),'run_id':str(run_id),'tool_id':str(tool_id),'limit':limit,
           'request_sha256':hashlib.sha256(tokens.canonical(request).encode()).hexdigest(),
           'expires_at':min(row['execution_deadline_at'],row['leased_until']).timestamp(),
           'policy_sha256':hashlib.sha256(tokens.canonical(tokens.settings()['models']).encode()).hexdigest()}
    prepared=tokens.RequestEnvelope('rag',{}, 'delegated',0,0,'delegated',value['request_sha256'])
    handle=await tokens.reserve(identity,prepared,parent=UUID(tool_id),held=limit)
    return encode(value),handle


@retry_transaction
async def start(identity,run_id,tool_id,request,ticket):
    from psycopg.types.json import Jsonb
    digest=hashlib.sha256(tokens.canonical(request).encode()).hexdigest()
    async with transaction() as conn:
        if ticket:
            expired=await (await conn.execute('SELECT clock_timestamp()>=to_timestamp(%s) AS expired',(ticket['expires_at'],))).fetchone()
            if expired['expired']:
                from contracts.errors import ApiError
                from contracts.v1 import ErrorCode
                raise ApiError(409,ErrorCode.INVALID_REQUEST,'Token allocation expired before admission')
        inserted=await (await conn.execute("INSERT INTO rag.search_operations(id,run_id,tool_call_id,request_sha256,ticket,status) VALUES (%s,%s,%s,%s,%s,'started') ON CONFLICT DO NOTHING RETURNING id",
            (identity,run_id,tool_id,digest,Jsonb(ticket) if ticket else None))).fetchone()
        if not inserted:
            row=await (await conn.execute('SELECT * FROM rag.search_operations WHERE id=%s',(identity,))).fetchone()
            if row['request_sha256']!=digest or row['run_id']!=run_id or row['tool_call_id']!=tool_id:raise ValueError('Search operation identity conflict')
            if row['status']=='completed':return row['result']
            from contracts.errors import ApiError
            from contracts.v1 import ErrorCode
            raise ApiError(409,ErrorCode.INTERRUPTED_UNKNOWN,'Search was already started; execution cannot be replayed')
    return None


@retry_transaction
async def finish(identity,result,*,status='completed'):
    from psycopg.types.json import Jsonb
    async with transaction() as conn:
        await conn.execute("UPDATE rag.search_operations SET status=%s,result=%s WHERE id=%s AND status='started'",(status,Jsonb(result),identity))


async def status(identity,run_id,tool_id):
    async with transaction(readonly=True) as conn:
        row=await (await conn.execute('SELECT status,result FROM rag.search_operations WHERE id=%s AND run_id=%s AND tool_call_id=%s',(identity,run_id,tool_id))).fetchone()
    return dict(row) if row else {'status':'absent','result':None}


async def reconcile(handle,summary):
    if handle and summary is not None:
        if any(type(summary.get(k)) is not int or summary[k]<0 for k in ('actual_input_tokens','actual_output_tokens','charged','unconfirmed_tokens')):
            raise ValueError('Invalid delegated usage report')
        if summary['actual_input_tokens']+summary['actual_output_tokens']>summary['charged']:raise ValueError('Delegated usage exceeds charge')
        await tokens.settle(handle,tokens.UsageRecord(int(summary['actual_input_tokens']),int(summary['actual_output_tokens']),
            'delegated' if summary['unconfirmed_tokens'] else 'provider',int(summary['charged'])))
        if summary.get('blocked'):
            await block_parent(handle)


@retry_transaction
async def block_parent(handle):
    from execution import store
    async with transaction() as conn:
        attempt=current_attempt()
        if attempt:await store.assert_active(conn,attempt.task,attempt.token)
        await conn.execute('UPDATE execution.token_budgets SET blocked=true WHERE namespace=%s AND id=(SELECT budget_id FROM execution.token_calls WHERE namespace=%s AND id=%s)',(handle[0],*handle))
        if attempt:await store.assert_active(conn,attempt.task,attempt.token)
