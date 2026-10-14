"""Short guarded persistence operations for acceptance and explicit user grants."""
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from psycopg.types.json import Jsonb
from execution import store
from execution.persistence import transaction, AlreadyApplied, Applied
from execution.contracts import TaskRef, TaskKind
from execution.retry import retry_transaction
from agent_service.acceptance import digest, validate_contract, verify_projection

def ref(run_id): return TaskRef(run_id, TaskKind.AGENT)

async def insert_grants(conn, run_id, grants):
    for ordinal, grant in enumerate(grants, 1):
        await conn.execute('INSERT INTO agent.run_tool_authorizations(id,run_id,ordinal,grant_data) VALUES (%s,%s,%s,%s)',
                           (uuid4(),run_id,ordinal,Jsonb(grant.model_dump() if hasattr(grant,'model_dump') else grant)))

@retry_transaction
async def load_grants(run_id, token):
    async with transaction('AGENT_DATABASE_URL',readonly=True) as conn:
        await store.assert_active(conn,ref(run_id),token,lock=False)
        rows=await (await conn.execute('SELECT a.id,a.grant_data,(SELECT count(*) FROM agent.tool_authorization_uses u WHERE u.authorization_id=a.id) AS uses FROM agent.run_tool_authorizations a WHERE a.run_id=%s ORDER BY ordinal',(run_id,))).fetchall()
        return [dict(id=str(r['id']),grant_data=r['grant_data'],uses=r['uses']) for r in rows]

@retry_transaction
async def load_goal(run_id,token):
    async with transaction('AGENT_DATABASE_URL',readonly=True) as conn:
        await store.assert_active(conn,ref(run_id),token,lock=False)
        row=await (await conn.execute('SELECT contract FROM agent.run_goal_contracts WHERE run_id=%s',(run_id,))).fetchone()
        return row['contract'] if row else {}

async def check_call(conn, run_id, call_id, schema, value, *, checkpoint=False, input_hash=None):
    batch=await (await conn.execute('SELECT * FROM agent.verification_batches WHERE run_id=%s AND call_id=%s',(run_id,call_id))).fetchone()
    if batch:
        from agent_service.verification_batches import combine
        if schema!='FinalEvaluation' or batch['value']!=value or combine(batch['parts'])!=value or input_hash!=batch['input_sha256']:
            raise ValueError('Grouped verification identity mismatch')
        if checkpoint:raise ValueError('Final verification has no graph recovery contract')
        await validate_batch_parts(conn,run_id,batch['parts'])
        return
    row=await (await conn.execute('SELECT status,purpose,output_summary,input_summary FROM agent.model_calls WHERE run_id=%s AND id=%s',(run_id,call_id))).fetchone()
    if not row or row['status'] != 'succeeded' or not isinstance(row['output_summary'],dict) or row['output_summary'].get('schema') != schema or row['output_summary'].get('payload_sha256') != digest(value):
        raise ValueError('Verification does not match settled model output')
    expected_purpose={'StructuredSummary':'context_compaction','GoalContract':'goal_analysis','StepEvaluation':'step_acceptance','ToolBatchEvaluation':'tool_intent','FinalEvaluation':'final_acceptance'}[schema]
    if row['purpose']!=expected_purpose: raise ValueError('Verification phase mismatch')
    if input_hash is not None and row['input_summary'].get('acceptance_input_sha256') != input_hash:
        raise ValueError('Verification does not match inspected inputs')
    if checkpoint:
        from agent_service.storage.receipts import anchor_valid
        receipts=await (await conn.execute('SELECT * FROM agent.call_checkpoint_receipts WHERE run_id=%s AND model_call_id=%s',(run_id,call_id))).fetchall()
        valid=False
        for receipt in receipts:
            if await anchor_valid(conn,receipt): valid=True; break
        if not valid: raise ValueError('Verification has no real checkpoint receipt')


async def validate_batch_parts(conn,run_id,parts):
    for part in parts:
        row=await (await conn.execute('SELECT * FROM agent.model_calls WHERE run_id=%s AND id=%s',(run_id,part['id']))).fetchone()
        if not row or row['status']!='succeeded' or row['purpose']!=part['purpose'] or row['output_summary'].get('schema')!='FinalEvaluation' or row['output_summary'].get('payload_sha256')!=digest(part['value']) or row['input_summary'].get('acceptance_input_sha256')!=part['input_sha256']:
            raise ValueError('Grouped verification differs from settled output')


@retry_transaction
async def confirm_compaction(run_id,token,call_id,value):
    async with transaction() as conn:
        await store.assert_active(conn,ref(run_id),token)
        await check_call(conn,run_id,call_id,'StructuredSummary',value,checkpoint=True)
        await store.assert_active(conn,ref(run_id),token)

@retry_transaction
async def save_goal(run_id, token, task, contract, call_id):
    contract=validate_contract(contract)
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        original=await (await conn.execute('SELECT task FROM agent.agent_runs WHERE id=%s',(run_id,))).fetchone()
        if original['task']!=task: raise ValueError('Goal contract original task mismatch')
        await check_call(conn,run_id,call_id,'GoalContract',contract,checkpoint=True)
        old=await (await conn.execute('SELECT * FROM agent.run_goal_contracts WHERE run_id=%s',(run_id,))).fetchone()
        if old:
            if old['task_sha256'] != digest(task) or old['contract'] != contract or str(old['model_call_id']) != str(call_id):
                raise ValueError('Goal contract conflict')
            return AlreadyApplied(run_id)
        await conn.execute('INSERT INTO agent.run_goal_contracts(run_id,task_sha256,contract,contract_sha256,model_call_id) VALUES (%s,%s,%s,%s,%s)',
                           (run_id,digest(task),Jsonb(contract),digest(contract),call_id))
        await store.assert_active(conn,ref(run_id),token)
    return Applied(run_id)

async def consume_grant(conn, run_id, authorization_id, call_id):
    row=await (await conn.execute('SELECT grant_data FROM agent.run_tool_authorizations WHERE run_id=%s AND id=%s',(run_id,authorization_id))).fetchone()
    if not row: raise ValueError('Authorization belongs to another Run')
    used=(await (await conn.execute('SELECT count(*) AS n FROM agent.tool_authorization_uses WHERE authorization_id=%s',(authorization_id,))).fetchone())['n']
    if used >= row['grant_data']['max_calls']: raise ValueError('Tool authorization exhausted')
    await conn.execute('INSERT INTO agent.tool_authorization_uses(run_id,authorization_id,tool_call_id) VALUES (%s,%s,%s)',(run_id,authorization_id,call_id))

async def write_verification(conn, run_id, phase, scope, inputs, payload, call_id):
    row=await (await conn.execute('SELECT input_sha256,payload,model_call_id FROM agent.run_verifications WHERE run_id=%s AND phase=%s AND scope_key=%s',(run_id,phase,scope))).fetchone()
    if row:
        if row['input_sha256'] != digest(inputs) or row['payload'] != payload or str(row['model_call_id']) != str(call_id):
            raise ValueError('Verification record conflict')
        return
    await conn.execute('INSERT INTO agent.run_verifications(id,run_id,phase,scope_key,input_sha256,payload,model_call_id) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                       (uuid5(NAMESPACE_URL,f'{run_id}:{phase}:{scope}'),run_id,phase,scope,digest(inputs),Jsonb(payload),call_id))

async def check_publication(conn, run_id, result, evidence, partial, *, proof_data=None):
    verify_projection(result)
    proof=result.proof
    if result.partial != partial or proof['inputs']['evidence'] != evidence:
        raise ValueError('Publication evidence or status changed')
    run=await (await conn.execute('SELECT task,mode FROM agent.agent_runs WHERE id=%s',(run_id,))).fetchone()
    if run['task'] != proof['inputs']['task']: raise ValueError('Publication original task mismatch')
    if run['mode']=='plan_execute' and not result.partial and await (await conn.execute("SELECT 1 FROM agent.run_steps WHERE run_id=%s AND status<>'completed' LIMIT 1",(run_id,))).fetchone():
        raise ValueError('Publication contains unfinished plan steps')
    goal=await (await conn.execute('SELECT contract FROM agent.run_goal_contracts WHERE run_id=%s',(run_id,))).fetchone()
    if goal and goal['contract'] != proof['inputs']['contract']: raise ValueError('Publication contract mismatch')
    if proof['evaluation'] is not None:
        if not goal: raise ValueError('No confirmed goal contract')
        await check_call(conn,run_id,proof['call_id'],'FinalEvaluation',proof['evaluation'],input_hash=proof['input_hash'])
    elif not result.partial or not proof['unavailable']:
        raise ValueError('Unverified publication cannot complete')
    payload=proof
    if proof_data is not None:
        import hashlib
        from agent_service.storage.bodies import reference,pin
        sha=hashlib.sha256(b'json\0'+proof_data).hexdigest()
        await conn.execute("INSERT INTO agent.execution_bodies(run_id,sha256,format,body,byte_length) VALUES (%s,%s,'json',%s,%s) ON CONFLICT DO NOTHING",(run_id,sha,proof_data,len(proof_data)))
        ref=reference(run_id,sha,'json',len(proof_data))
        await pin(conn,run_id,ref,'verification:final')
        payload={k:v for k,v in proof.items() if k!='inputs'}
        payload['inputs_ref']=ref
    await write_verification(conn,run_id,'final','result',proof['inputs'],payload,proof['call_id'])

@retry_transaction
async def confirm_intents(run_id,token,task,contract,message,intents,evaluation,*,dispatch):
    bindings=dict(task=task,contract=contract,triggering=message.additional_kwargs.get('_execution_call'),calls=message.tool_calls)
    key=digest(bindings)
    ids={c['id'] for c in message.tool_calls}
    if set(intents)!=ids or any(d['batch_hash']!=key for d in intents.values()): raise ValueError('Intent scope changed')
    call_ids={d['evaluation_call_id'] for d in intents.values()}
    if len(call_ids)!=1: raise ValueError('Intent call mismatch')
    cid=next(iter(call_ids))
    allowed=all(d['allowed'] for d in intents.values())
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        if allowed:
            if evaluation is None or cid is None: raise ValueError('No independent intent evaluation')
            decisions=evaluation['decisions']
            requirements={r['id'] for r in contract['requirements']}
            if len(decisions)!=len(ids) or {d['tool_call_id'] for d in decisions}!=ids or any(d['status']!='allow' or not d['requirement_ids'] or not set(d['requirement_ids'])<=requirements for d in decisions):
                raise ValueError('Intent evaluation did not allow the entire batch')
            await check_call(conn,run_id,cid,'ToolBatchEvaluation',evaluation,checkpoint=True,input_hash=key)
        elif dispatch: raise ValueError('Tool batch was rejected')
        await write_verification(conn,run_id,'tool',str(message.additional_kwargs['_execution_call']['id']),bindings,
                                 dict(intents=intents,evaluation=evaluation),cid)
        await store.assert_active(conn,ref(run_id),token)
