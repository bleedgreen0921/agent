import hashlib
import json
import os
from uuid import UUID, uuid4
from agent_service.versions import GRAPH_VERSION
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, RunCreate
from execution.persistence import transaction as connect
from execution import store
from execution.contracts import TaskKind, TaskRef
from execution.retry import retry_transaction
TERMINAL = {'completed', 'partial', 'failed', 'cancelled'}

def validate_idempotency_key(key: str | None) -> None:
    if key is not None and (not 1 <= len(key) <= 128 or any((ord(c) < 32 or ord(c) > 126 for c in key))):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Invalid Idempotency-Key')

def request_digest(body: RunCreate) -> bytes:
    payload = body.model_dump()
    if not payload['tool_authorizations']: payload.pop('tool_authorizations')
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).digest()

def lock_id(key: str) -> int:
    return int.from_bytes(hashlib.sha256(f'run:{key}'.encode()).digest()[:8], 'big', signed=True)

@retry_transaction
async def create_run(body: RunCreate, key: str | None) -> tuple[dict, bool]:
    validate_idempotency_key(key)
    digest = request_digest(body)
    queue_seconds = int(os.environ.get('AGENT_QUEUE_TIMEOUT_SECONDS', '60'))
    if queue_seconds < 1:
        raise RuntimeError('AGENT_QUEUE_TIMEOUT_SECONDS must be positive')
    run_id = uuid4()
    async with connect('AGENT_DATABASE_URL') as conn:
        if key:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock_id(key),))
            existing = await (await conn.execute('SELECT id,status,created_at,request_digest FROM agent.run_state WHERE idempotency_key=%s FOR UPDATE', (key,))).fetchone()
            if existing:
                if bytes(existing['request_digest']) != digest:
                    raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, 'Idempotency key conflict')
                return ({'run_id': str(existing['id']), 'status': existing['status'], 'created_at': existing['created_at']}, True)
        from agent_service.intent import validate_grants
        try: validate_grants(body.tool_authorizations)
        except ValueError as exc: raise ApiError(422, ErrorCode.INVALID_REQUEST, str(exc)) from exc
        created = await store.create(conn, TaskRef(run_id, TaskKind.AGENT), queue_seconds=queue_seconds)
        await conn.execute('INSERT INTO agent.agent_runs(id,task,mode,request_digest,idempotency_key,graph_version)\n            VALUES (%s,%s,%s,%s,%s,%s)', (run_id, body.task, body.mode, digest, key, GRAPH_VERSION))
        from agent_service.storage.acceptance import insert_grants
        await insert_grants(conn,run_id,body.tool_authorizations)
    return ({'run_id': str(run_id), 'status': 'queued', 'created_at': created['created_at']}, False)

async def get_run(run_id: UUID) -> dict:
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        row = await (await conn.execute('SELECT id,mode,status,created_at,finished_at,error_code FROM agent.run_state\n            WHERE id=%s', (run_id,))).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, 'Run not found')
        from execution.progress import ProgressStore
        progress = await ProgressStore().snapshot(conn, run_id)
        result = None
        if row['status'] in {'completed', 'partial'}:
            result = await load_result(conn, run_id)
    error = {'code': row['error_code'], 'message': public_error_message(row['error_code'])} if row['error_code'] else None
    from execution.tokens import report
    return {'run_id': str(row['id']), 'mode': row['mode'], 'status': row['status'], 'created_at': row['created_at'], 'finished_at': row['finished_at'], 'result': result, 'error': error, 'progress': progress,'token_usage':await report(run_id)}

async def load_result(conn, run_id: UUID) -> dict | None:
    result = await (await conn.execute('SELECT id,answer,notices,acceptance FROM agent.run_results WHERE run_id=%s', (run_id,))).fetchone()
    if not result:
        return None
    claims = await (await conn.execute('SELECT id,text,support,reason FROM agent.result_claims WHERE result_id=%s ORDER BY ordinal', (result['id'],))).fetchall()
    snapshots = await (await conn.execute('SELECT evidence_id,document_id,document_version_id,title,content,source_locator\n        FROM agent.evidence_snapshots WHERE result_id=%s ORDER BY evidence_id', (result['id'],))).fetchall()
    evidence_by_claim = {claim['id']: [] for claim in claims}
    relations = await (await conn.execute('SELECT claim_id,evidence_id FROM agent.claim_evidence WHERE result_id=%s ORDER BY evidence_id', (result['id'],))).fetchall()
    for relation in relations:
        evidence_by_claim[relation['claim_id']].append(relation['evidence_id'])
    return {'answer': result['answer'], 'claims': [{'text': claim['text'], 'support': claim['support'], 'evidence_ids': evidence_by_claim[claim['id']], 'reason': claim['reason']} for claim in claims], 'citations': [dict(snapshot) for snapshot in snapshots], 'notices': result['notices'], 'acceptance': result['acceptance']}

def public_error_message(code: str) -> str:
    if code=='INPUT_TOKEN_LIMIT_EXCEEDED':return 'The original request exceeds the configured model input budget'
    if code == 'CPU_POOL_RESET': return 'The shared CPU executor interrupted the computation'
    return {'MODEL_RATE_LIMITED': 'The model service is rate limited', 'EXECUTION_STALLED': 'Execution stopped because a phase made no progress', 'TOOL_BATCH_TOO_LARGE': 'The model requested too many tools at once', 'TOOL_CAPACITY_EXCEEDED': 'Tool execution capacity is full', 'QUEUE_TIMEOUT': 'Run expired before execution started', 'RUN_TIMEOUT': 'Run exceeded its execution deadline', 'MODEL_AUTH_FAILED': 'The model service rejected authentication', 'MODEL_CALL_FAILED': 'The model service failed', 'MODEL_TIMEOUT': 'The model request timed out', 'TOOL_CALL_FAILED': 'A tool provider failed', 'INVALID_PLAN': 'The generated plan was invalid', 'AUTH_FAILED': 'A service authentication check failed', 'QUOTA_EXCEEDED': 'The run call budget was exhausted', 'INTERRUPTED_UNKNOWN': 'Execution stopped with an unresolved external call'}.get(code, 'Run failed')

@retry_transaction(profile='control')
async def cancel_run(run_id: UUID) -> tuple[dict, int]:
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        row = await store.stop(conn, TaskRef(run_id, TaskKind.AGENT))
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, 'Run not found')
    status = 'cancelling' if row['status'] == 'stopping' else row['status']
    return ({'run_id': str(run_id), 'status': status}, 202 if status == 'cancelling' else 200)

async def trace(run_id: UUID) -> dict:
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        run = await (await conn.execute('SELECT id,mode,status,created_at,started_at,finished_at,error_code,\n            model_calls_used,tool_calls_used,model_call_limit,tool_call_limit,termination_reason\n            FROM agent.run_state WHERE id=%s', (run_id,))).fetchone()
        if not run:
            raise ApiError(404, ErrorCode.NOT_FOUND, 'Run not found')
        steps = await (await conn.execute('SELECT * FROM agent.run_steps WHERE run_id=%s ORDER BY ordinal', (run_id,))).fetchall()
        models = await (await conn.execute('SELECT * FROM agent.model_calls WHERE run_id=%s ORDER BY started_at,id', (run_id,))).fetchall()
        tools = await (await conn.execute('SELECT * FROM agent.tool_calls WHERE run_id=%s ORDER BY started_at,id', (run_id,))).fetchall()
    return {'run': dict(run), 'steps': [dict(row) for row in steps], 'model_calls': [{k: v for k, v in row.items() if k not in {'lease_token', 'attempt_token'}} for row in models], 'tool_calls': [{k: v for k, v in row.items() if k not in {'lease_token', 'attempt_token'}} for row in tools]}

async def timeline(run_id: UUID) -> dict:
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        run = await (await conn.execute('SELECT id,mode,status,created_at,started_at,finished_at,error_code,termination_reason,recovery_protocol\n            FROM agent.run_state WHERE id=%s', (run_id,))).fetchone()
        if not run:
            raise ApiError(404, ErrorCode.NOT_FOUND, 'Run not found')
        manifest = await (await conn.execute('SELECT schema_version,manifest,captured_at FROM agent.run_manifests WHERE run_id=%s', (run_id,))).fetchone()
        steps = await (await conn.execute('SELECT id,ordinal,goal,completion_condition,status,result_summary,\n            started_at,finished_at,error_code FROM agent.run_steps\n            WHERE run_id=%s AND started_at IS NOT NULL', (run_id,))).fetchall()
        models = await (await conn.execute('SELECT id,step_id,purpose,model,status,started_at,finished_at,\n            input_summary,output_summary,input_tokens,output_tokens,error_code,checkpointed\n            FROM agent.model_calls WHERE run_id=%s', (run_id,))).fetchall()
        tools = await (await conn.execute('SELECT id,step_id,triggering_model_call_id,tool_name,status,started_at,finished_at,\n            argument_summary,result_summary,service_request_id,retrieval_id,evidence_ids,error_code,checkpointed\n            FROM agent.tool_calls WHERE run_id=%s', (run_id,))).fetchall()
        receipts=await (await conn.execute('SELECT model_call_id,tool_call_id,source,thread_id,checkpoint_ns,checkpoint_id,channel,confirmed_at FROM agent.call_checkpoint_receipts WHERE run_id=%s ORDER BY confirmed_at,checkpoint_id,channel',(run_id,))).fetchall()
        verifications=await (await conn.execute('SELECT id,phase,scope_key,created_at,payload FROM agent.run_verifications WHERE run_id=%s ORDER BY created_at,id',(run_id,))).fetchall()
        result = await (await conn.execute('SELECT r.id,r.created_at,\n            (SELECT count(*) FROM agent.result_claims c WHERE c.result_id=r.id) AS claim_count,\n            (SELECT count(*) FROM agent.evidence_snapshots e WHERE e.result_id=r.id) AS citation_count,\n            jsonb_array_length(r.notices) AS notice_count\n            FROM agent.run_results r WHERE r.run_id=%s', (run_id,))).fetchone()
    run_entity = str(run['id'])
    events: list[dict] = [{'event_id': f'run:{run_entity}:created', 'type': 'run_created', 'occurred_at': run['created_at'], 'finished_at': None, 'status': 'queued', 'entity_id': run_entity, 'step_id': None, 'parent_id': None, 'name': None, 'details': {'mode': run['mode']}}]
    if manifest:
        events.append({'event_id': f'run:{run_entity}:manifest', 'type': 'manifest_captured', 'occurred_at': manifest['captured_at'], 'finished_at': None, 'status': 'captured', 'entity_id': run_entity, 'step_id': None, 'parent_id': None, 'name': None, 'details': {'schema_version': manifest['schema_version']}})
    if run['started_at']:
        events.append({'event_id': f'run:{run_entity}:started', 'type': 'run_started', 'occurred_at': run['started_at'], 'finished_at': None, 'status': 'running', 'entity_id': run_entity, 'step_id': None, 'parent_id': None, 'name': None, 'details': {}})
    for row in steps:
        entity_id = str(row['id'])
        events.append({'event_id': f'step:{entity_id}', 'type': 'step', 'occurred_at': row['started_at'], 'finished_at': row['finished_at'], 'status': row['status'], 'entity_id': entity_id, 'step_id': entity_id, 'parent_id': None, 'name': None, 'details': {'ordinal': row['ordinal'], 'goal': row['goal'], 'completion_condition': row['completion_condition'], 'result_summary': row['result_summary'], 'error_code': row['error_code']}})
    for row in models:
        entity_id = str(row['id'])
        events.append({'event_id': f'model:{entity_id}', 'type': 'model_call', 'occurred_at': row['started_at'], 'finished_at': row['finished_at'], 'status': row['status'], 'entity_id': entity_id, 'step_id': str(row['step_id']) if row['step_id'] else None, 'parent_id': None, 'name': row['model'], 'details': {'purpose': row['purpose'], 'model': row['model'], 'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'], 'input_summary': row['input_summary'], 'output_summary': row['output_summary'], 'error_code': row['error_code'], 'checkpointed': row['checkpointed']}})
    for row in tools:
        entity_id = str(row['id'])
        events.append({'event_id': f'tool:{entity_id}', 'type': 'tool_call', 'occurred_at': row['started_at'], 'finished_at': row['finished_at'], 'status': row['status'], 'entity_id': entity_id, 'step_id': str(row['step_id']) if row['step_id'] else None, 'parent_id': str(row['triggering_model_call_id']) if row['triggering_model_call_id'] else None, 'name': row['tool_name'], 'details': {'tool_name': row['tool_name'], 'argument_summary': row['argument_summary'], 'result_summary': row['result_summary'], 'service_request_id': row['service_request_id'], 'retrieval_id': row['retrieval_id'], 'evidence_ids': row['evidence_ids'], 'error_code': row['error_code'], 'checkpointed': row['checkpointed']}})
    by_call={}
    for receipt in receipts:
        key=str(receipt['model_call_id'] or receipt['tool_call_id'])
        by_call.setdefault(key,[]).append({k:v for k,v in receipt.items() if k not in {'model_call_id','tool_call_id'}})
    for event in events:
        if event['type'] in {'model_call','tool_call'}:
            event['details']['checkpoint_receipts']=by_call.get(event['entity_id'],[])
    events[0]['details']['recovery_protocol']=run['recovery_protocol']
    if result:
        entity_id = str(result['id'])
        events.append({'event_id': f'result:{entity_id}', 'type': 'result_published', 'occurred_at': result['created_at'], 'finished_at': None, 'status': 'published', 'entity_id': entity_id, 'step_id': None, 'parent_id': None, 'name': None, 'details': {'claim_count': result['claim_count'], 'citation_count': result['citation_count'], 'notice_count': result['notice_count']}})
    for v in verifications:
        payload=v['payload'];phase=v['phase']
        summary={'phase':phase,'scope_key':v['scope_key']}
        if phase=='tool': summary['allowed']=all(d['allowed'] for d in payload['intents'].values())
        elif phase=='step': summary['status']=payload.get('status','failed')
        else:
            summary['evaluation_present']=payload.get('evaluation') is not None
            summary['policy_version']=payload['policy_version']
        events.append({'event_id':f"verification:{v['id']}",'type':'verification','occurred_at':v['created_at'],'finished_at':None,'status':'recorded','entity_id':str(v['id']),'step_id':v['scope_key'] if phase=='step' else None,'parent_id':None,'name':None,'details':summary})
    if run['finished_at']:
        events.append({'event_id': f'run:{run_entity}:finished', 'type': 'run_finished', 'occurred_at': run['finished_at'], 'finished_at': run['finished_at'], 'status': run['status'], 'entity_id': run_entity, 'step_id': None, 'parent_id': None, 'name': None, 'details': {'termination_reason': run['termination_reason'], 'error_code': run['error_code']}})
    priority = {'run_created': 0, 'manifest_captured': 1, 'run_started': 2, 'step': 3, 'model_call': 4, 'tool_call': 5, 'result_published': 6, 'run_finished': 8,'verification':6}
    events.sort(key=lambda item: (item['occurred_at'], priority[item['type']], item['entity_id'] or ''))
    for sequence, event in enumerate(events, 1):
        event['sequence'] = sequence
    return {'run_id': run_entity, 'status': run['status'], 'manifest': {'schema_version': manifest['schema_version'], 'captured_at': manifest['captured_at'], 'data': manifest['manifest']} if manifest else None, 'events': events}


async def run_identity(run_id):
    async with connect('AGENT_DATABASE_URL',readonly=True) as conn:
        row=await (await conn.execute('SELECT id FROM agent.agent_runs WHERE id=%s',(run_id,))).fetchone()
    if row is None:raise LookupError('Run not found')
    return row['id']
