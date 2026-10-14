"""Persistent graph metadata; no framework, file, or model operations."""
from uuid import uuid4
from execution.persistence import transaction
from execution import store
from execution.contracts import TaskRef,TaskKind
from execution.retry import retry_transaction
from psycopg.types.json import Jsonb


def ref(run_id):return TaskRef(run_id,TaskKind.AGENT)


@retry_transaction
async def prepare_steps(run_id,token,steps):
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        for ordinal,step in enumerate(steps,1):
            await conn.execute("INSERT INTO agent.run_steps(id,run_id,ordinal,goal,completion_condition,requirement_ids,status) VALUES (%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT(run_id,ordinal) DO NOTHING",(uuid4(),run_id,ordinal,step['goal'],step['completion_condition'],Jsonb(step.get('requirement_ids',[]))))
            actual=await (await conn.execute('SELECT goal,completion_condition,requirement_ids FROM agent.run_steps WHERE run_id=%s AND ordinal=%s',(run_id,ordinal))).fetchone()
            if actual!=dict(goal=step['goal'],completion_condition=step['completion_condition'],requirement_ids=step.get('requirement_ids',[])):
                raise ValueError('Step definition conflict')
        await store.assert_active(conn,ref(run_id),token)


@retry_transaction
async def start_step(run_id,token,ordinal):
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        row=await (await conn.execute("UPDATE agent.run_steps SET status=CASE WHEN status='completed' THEN status ELSE 'running' END,started_at=COALESCE(started_at,clock_timestamp()) WHERE run_id=%s AND ordinal=%s RETURNING id",(run_id,ordinal))).fetchone()
        await store.assert_active(conn,ref(run_id),token)
        return row


@retry_transaction
async def complete_step(run_id,token,step_id,evaluation,call_id,task,evidence,contract):
    from agent_service.acceptance import digest, validate_summary
    from agent_service.storage.acceptance import check_call,write_verification
    if evaluation['status']!='met': raise ValueError('Step has not passed acceptance')
    validate_summary(evaluation['summary'],task,evidence,contract)
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        step=await (await conn.execute('SELECT completion_condition FROM agent.run_steps WHERE run_id=%s AND id=%s',(run_id,step_id))).fetchone()
        if not step: raise ValueError('Step not in Run')
        inputs=dict(task=task,evidence=evidence,contract=contract,step_id=str(step_id),completion_condition=step['completion_condition'])
        await check_call(conn,run_id,call_id,'StepEvaluation',evaluation,checkpoint=True,input_hash=digest(inputs))
        owner=await (await conn.execute('SELECT step_id FROM agent.model_calls WHERE id=%s AND run_id=%s',(call_id,run_id))).fetchone()
        if not owner or owner['step_id']!=step_id: raise ValueError('Step evaluation belongs to another step')
        await write_verification(conn,run_id,'step',str(step_id),inputs,evaluation,call_id)
        await conn.execute("UPDATE agent.run_steps SET status='completed',result_summary=%s,finished_at=COALESCE(finished_at,clock_timestamp()) WHERE id=%s AND run_id=%s",('sha256='+digest(evaluation['summary']),step_id,run_id))
        await store.assert_active(conn,ref(run_id),token)

@retry_transaction
async def reject_step(run_id,token,step_id,reason,evaluation=None,call_id=None):
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        from agent_service.acceptance import digest
        from agent_service.storage.acceptance import write_verification
        payload=dict(status='failed',reason=reason,evaluation=evaluation)
        await write_verification(conn,run_id,'step',str(step_id),{'step_id':str(step_id)},payload,call_id)
        await conn.execute("UPDATE agent.run_steps SET status='failed',error_code='STEP_ACCEPTANCE_FAILED',result_summary=%s,finished_at=clock_timestamp() WHERE id=%s AND run_id=%s AND status<>'completed'",('sha256='+digest(payload),step_id,run_id))
        await conn.execute("UPDATE agent.run_steps SET status='skipped',error_code='STEP_ACCEPTANCE_FAILED',finished_at=clock_timestamp() WHERE run_id=%s AND status='pending'",(run_id,))
        await store.assert_active(conn,ref(run_id),token)


@retry_transaction
async def fail_steps(run_id,token):
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,ref(run_id),token)
        await conn.execute("UPDATE agent.run_steps SET status='failed',error_code='QUOTA_EXCEEDED',finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",(run_id,))
        await store.assert_active(conn,ref(run_id),token)


@retry_transaction
async def load_run(run_id,token):
    async with transaction('AGENT_DATABASE_URL',readonly=True) as conn:
        return await (await conn.execute("SELECT r.task,r.mode,m.manifest FROM agent.run_state r LEFT JOIN agent.run_manifests m ON m.run_id=r.id WHERE r.id=%s AND r.lease_token=%s AND r.status='running'",(run_id,token))).fetchone()
