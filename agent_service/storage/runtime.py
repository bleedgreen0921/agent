import os
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from agent_service.manifest import execution_manifest, schema_version
from agent_service.timeouts import configured_timeouts
from execution.persistence import transaction as connect
from execution import store
from execution.contracts import TaskRef, TaskKind, TERMINAL
from execution.persistence import Applied, AlreadyApplied, Rejected, PersistenceOutcomeUnknown
from execution.retry import retry_transaction
from execution.reconciliation import OperationIdentity, Resolution, ResolutionState, verify_unknown

def task_ref(run_id):
    return TaskRef(run_id, TaskKind.AGENT)

async def claim_run(worker_id: UUID | None=None) -> dict | None:
    configured_timeouts()
    from execution.persistence import resources, deadline
    runtime = resources()
    settings = getattr(runtime, 'agent_settings', None)
    manifest = settings['manifest'] if settings else await runtime.io.run(execution_manifest, deadline=deadline(30))
    return await _claim_run(worker_id, manifest)


@retry_transaction(profile='control')
async def _claim_run(worker_id, manifest):
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        await conn.execute('SELECT pg_advisory_xact_lock(824731091)')
        await store.sweep(conn, TaskKind.AGENT)
        running = (await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
            SELECT count(*) AS n FROM execution.tasks CROSS JOIN clock WHERE kind='agent_run'
            AND status IN ('running','stopping') AND leased_until>clock.at
            AND execution_deadline_at>clock.at""")).fetchone())['n']
        if running >= int(os.environ.get('AGENT_MAX_CONCURRENT_RUNS', '2')):
            return None
        row = await store.claim(conn, TaskKind.AGENT, lease_seconds=int(os.environ.get('AGENT_LEASE_SECONDS', '120')), worker_id=worker_id, sweep_expired=False)
        if not row:
            return None
        first = row['attempts'] == 1
        if not first:
            from agent_service.startup import compatible
            from agent_service.versions import GRAPH_VERSION, MANIFEST_SCHEMA_VERSION
            saved = await (await conn.execute('''SELECT m.manifest,m.schema_version,r.graph_version,r.recovery_protocol
                FROM agent.agent_runs r LEFT JOIN agent.run_manifests m ON m.run_id=r.id WHERE r.id=%s''', (row['id'],))).fetchone()
            if not saved or saved['schema_version'] != MANIFEST_SCHEMA_VERSION or saved['graph_version'] != GRAPH_VERSION or saved['recovery_protocol'] != 2 or not compatible(saved['manifest'], manifest):
                await store.settle_failure(conn, task_ref(row['id']), row['lease_token'], 'INTERRUPTED_UNKNOWN', reason='configuration_mismatch')
                await interrupt_calls(conn, row['id'])
                return None
        if first:
            await conn.execute('INSERT INTO agent.run_manifests(run_id,schema_version,manifest,captured_at)\n                VALUES (%s,%s,%s,clock_timestamp())', (row['id'], schema_version(), Jsonb(manifest)))
        await store.assert_active(conn, task_ref(row['id']), row['lease_token'])
        return {'run_id': row['id'], 'lease_token': row['lease_token'], 'first_claim': first, 'deadline_at': row['execution_deadline_at'], 'remaining_seconds': row['remaining_seconds']}

async def safe_to_resume(conn, run_id: UUID) -> bool:
    from agent_service.storage.receipts import resumable
    return await resumable(conn, run_id)

async def interrupt_calls(conn, run_id, _code=None):
    await conn.execute("UPDATE agent.run_steps SET status='failed',error_code=%s,finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",(_code or 'WORKER_INTERRUPTED',run_id))
    for table in ('model_calls', 'tool_calls'):
        await conn.execute(f"UPDATE agent.{table} SET status='interrupted_unknown',finished_at=clock_timestamp(),error_code='INTERRUPTED_UNKNOWN' WHERE run_id=%s AND status='started'", (run_id,))

async def heartbeat(run_id: UUID, token: UUID) -> bool:
    return await store.renew(task_ref(run_id), token, int(os.environ.get('AGENT_LEASE_SECONDS', '120')))

async def control_state(run_id: UUID, token: UUID) -> str:
    row = await store.control_snapshot(task_ref(run_id))
    refusal = store.control_refusal(task_ref(run_id), token, row)
    return 'running' if refusal is None else {'user_cancel': 'cancel', 'deadline': 'timeout'}.get(refusal, 'lost')

@retry_transaction(profile='control')
async def revoke_and_finish(run_id: UUID, token: UUID, reason: str) -> None:
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        result = await store.settle_failure(conn, task_ref(run_id), token, 'RUN_TIMEOUT', worker_exit=True)
        if result.accepted:
            await interrupt_calls(conn, run_id)

@retry_transaction(profile='control')
async def attempt_exited(run_id: UUID, token: UUID) -> None:
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE', (run_id, TaskKind.AGENT))).fetchone()
        if not row or row['lease_token'] != token or row['status'] in TERMINAL:
            return
        safe = await safe_to_resume(conn, run_id)
        result = await store.settle_failure(conn, task_ref(run_id), token, 'WORKER_INTERRUPTED' if safe else 'INTERRUPTED_UNKNOWN', retryable=safe, reason='worker_exit', worker_exit=True)
        if result.accepted and result.status in TERMINAL:
            await interrupt_calls(conn, run_id)

async def reserve_model(run_id: UUID, token: UUID, purpose: str, model: str, step_id: UUID | None=None, final: bool=False, input_summary: dict | None=None, *, call_id: UUID | None=None) -> UUID:
    return await _reserve(run_id, token, 'model', purpose, model, step_id, final, input_summary=input_summary, call_id=call_id or uuid4())

async def reserve_tool(run_id: UUID, token: UUID, tool_name: str, arguments: dict, step_id: UUID | None=None, triggering_model_call_id: UUID | None=None, *, call_id: UUID | None=None) -> UUID:
    return await _reserve(run_id, token, 'tool', tool_name, '', step_id, False, arguments=arguments, triggering_model_call_id=triggering_model_call_id, call_id=call_id or uuid4())

async def _reserve(run_id: UUID, token: UUID, kind: str, purpose: str, model: str, step_id: UUID | None, final: bool, *, input_summary: dict | None=None, arguments: dict | None=None, triggering_model_call_id: UUID | None=None, call_id: UUID | None=None, outcome=False):
    if call_id is None:
        raise ValueError('A stable call ID must be allocated before reservation')
    try:
        return await _reserve_transaction(run_id, token, kind, purpose, model, step_id, final, input_summary=input_summary,
            arguments=arguments, triggering_model_call_id=triggering_model_call_id, call_id=call_id, outcome=outcome)
    except PersistenceOutcomeUnknown as exc:
        operation = OperationIdentity('reserve_' + kind, task_ref(run_id), call_id, token)
        expected = {'step_id': step_id, **({'purpose': purpose, 'model': model, 'input_summary': input_summary or {}}
            if kind == 'model' else {'tool_name': purpose, 'argument_summary': arguments or {}, 'triggering_model_call_id': triggering_model_call_id})}
        await verify_unknown(exc, operation, lambda: reconcile_operation(operation, expected))
        # Even a recorded reservation says nothing about external dispatch.
        raise


@retry_transaction
async def _reserve_transaction(run_id, token, kind, purpose, model, step_id, final, *, input_summary, arguments, triggering_model_call_id, call_id, outcome):
    async with connect('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn, task_ref(run_id), token)
        table = 'model_calls' if kind=='model' else 'tool_calls'
        existing = await (await conn.execute(f'SELECT * FROM agent.{table} WHERE id=%s',(call_id,))).fetchone()
        if existing:
            identity = (existing['run_id']==run_id and existing['attempt_token']==token and existing['step_id']==step_id)
            same = existing['purpose']==purpose and existing['model']==model and existing['input_summary']==(input_summary or {}) if kind=='model' else existing['tool_name']==purpose and existing['argument_summary']==(arguments or {}) and existing['triggering_model_call_id']==triggering_model_call_id
            if not identity or not same: raise ValueError('Call reservation identity conflict')
            return AlreadyApplied(call_id) if outcome else call_id
        run = await (await conn.execute('SELECT model_calls_used,tool_calls_used,model_call_limit,tool_call_limit\n            FROM agent.agent_runs WHERE id=%s FOR UPDATE', (run_id,))).fetchone()
        used = run[f'{kind}_calls_used']
        limit = run[f'{kind}_call_limit']
        allowed = used < limit if kind == 'tool' or purpose == 'final_acceptance' else used < limit - (1 if purpose in ('final','final_acceptance_part') else 2)
        if kind == 'model' and final and purpose not in ('final','final_acceptance','final_acceptance_part'):
            raise ValueError('Final phase must have an explicit final purpose')
        if kind == 'model' and purpose in ('final','final_acceptance'):
            prior = await (await conn.execute('SELECT 1 FROM agent.model_calls WHERE run_id=%s AND purpose=%s LIMIT 1',(run_id,purpose))).fetchone()
            if prior: raise ValueError('Final phase cannot be replayed')
            if purpose=='final_acceptance':
                generated=await (await conn.execute("SELECT 1 FROM agent.model_calls WHERE run_id=%s AND purpose='final' AND status='succeeded'",(run_id,))).fetchone()
                if not generated: raise ValueError('Final acceptance requires settled generation')
        if not allowed:
            raise BudgetExhausted(kind)
        await conn.execute(f'UPDATE agent.agent_runs SET {kind}_calls_used={kind}_calls_used+1 WHERE id=%s', (run_id,))
        if kind == 'model':
            await conn.execute("INSERT INTO agent.model_calls(id,run_id,step_id,purpose,model,status,input_summary,attempt_token,started_at)\n                VALUES (%s,%s,%s,%s,%s,'started',%s,%s,clock_timestamp())", (call_id, run_id, step_id, purpose, model, Jsonb(input_summary or {}), token))
        else:
            await conn.execute("INSERT INTO agent.tool_calls(id,run_id,step_id,triggering_model_call_id,tool_name,status,argument_summary,attempt_token,started_at)\n                VALUES (%s,%s,%s,%s,%s,'started',%s,%s,clock_timestamp())", (call_id, run_id, step_id, triggering_model_call_id, purpose, Jsonb(arguments or {}), token))
            manifest_row=await (await conn.execute('SELECT manifest FROM agent.run_manifests WHERE run_id=%s',(run_id,))).fetchone()
            configured=[t for p in manifest_row['manifest'].get('tools',{}).get('providers',[]) for t in p['tools']] if manifest_row else []
            declared=next((t for t in configured if t['name']==purpose),None)
            if declared and declared['kind']=='side_effect':
                spec=declared['intent_spec']
                if not spec: raise ValueError('Side-effect tool has no frozen intent policy')
                grant_id=(arguments or {}).get('authorization_id')
                grant_row=await (await conn.execute('SELECT grant_data FROM agent.run_tool_authorizations WHERE run_id=%s AND id=%s',(run_id,grant_id))).fetchone() if grant_id else None
                if not grant_row: raise ValueError('Side-effect reservation has no explicit authorization')
                grant=grant_row['grant_data'];effects=(arguments or {}).get('effect_arguments',{})
                from agent_service.acceptance import digest
                if grant['tool_name']!=purpose or grant['operation']!=spec['operation'] or set(grant['exact_arguments'])!=set(spec['effect_fields']) or set(effects)!=set(spec['effect_fields']) or any(digest(effects[k])!=digest(v) for k,v in grant['exact_arguments'].items()):
                    raise ValueError('Side-effect reservation exceeds authorization scope')
                from agent_service.storage.acceptance import consume_grant
                await consume_grant(conn,run_id,grant_id,call_id)
        await store.assert_active(conn, task_ref(run_id), token)
    return Applied(call_id) if outcome else call_id

class BudgetExhausted(RuntimeError):
    pass

async def _settle(run_id, token, call_id, table, assignments, values):
    try:
        return await _settle_transaction(run_id, token, call_id, table, assignments, values)
    except PersistenceOutcomeUnknown as exc:
        kind = 'model' if table == 'model_calls' else 'tool'
        operation = OperationIdentity('settle_' + kind, task_ref(run_id), call_id, token)
        expected = dict(zip((assignment.split('=')[0] for assignment in assignments.split(',')),
                            (value.obj if isinstance(value, Jsonb) else value for value in values)))
        resolved = await verify_unknown(exc, operation, lambda: reconcile_operation(operation, expected))
        if resolved.state == ResolutionState.APPLIED:
            return AlreadyApplied(resolved.value)
        raise


@retry_transaction
async def _settle_transaction(run_id, token, call_id, table, assignments, values):
    if table not in {'model_calls','tool_calls'}: raise ValueError('Unknown call kind')
    try:
        async with connect('AGENT_DATABASE_URL') as conn:
            await conn.execute('SELECT id FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE',(run_id,TaskKind.AGENT))
            current = await (await conn.execute(f'SELECT * FROM agent.{table} WHERE id=%s AND run_id=%s FOR UPDATE',(call_id,run_id))).fetchone()
            if not current or current['attempt_token'] != token: return Rejected('call_owner')
            columns=[assignment.split('=')[0] for assignment in assignments.split(',')]
            plain=[value.obj if isinstance(value,Jsonb) else value for value in values]
            if current['status']!='started':
                if all(current[column]==value for column,value in zip(columns,plain)):
                    return AlreadyApplied(call_id)
                await store.assert_active(conn,task_ref(run_id),token)
                raise ValueError('Call settlement conflict')
            await store.assert_active(conn,task_ref(run_id),token)
            await conn.execute(f"UPDATE agent.{table} SET {assignments},finished_at=clock_timestamp() WHERE id=%s AND run_id=%s AND attempt_token=%s AND status='started'",(*values,call_id,run_id,token))
            await store.assert_active(conn,task_ref(run_id),token)
        return Applied(call_id)
    except PermissionError:
        return Rejected('execution_authority')

async def settle_model(run_id: UUID, token: UUID, call_id: UUID, status: str, *, input_tokens=None, output_tokens=None, output_summary=None, error_code=None) -> Applied | Rejected:
    return await _settle(run_id, token, call_id, 'model_calls', 'status=%s,input_tokens=%s,output_tokens=%s,output_summary=%s,error_code=%s', (status, input_tokens, output_tokens, Jsonb(output_summary) if output_summary is not None else None, error_code))

async def settle_tool(run_id: UUID, token: UUID, call_id: UUID, status: str, *, result_summary=None, service_request_id=None, retrieval_id=None, evidence_ids=None, error_code=None) -> Applied | Rejected:
    return await _settle(run_id, token, call_id, 'tool_calls', 'status=%s,result_summary=%s,service_request_id=%s,retrieval_id=%s,evidence_ids=%s,error_code=%s', (status, Jsonb(result_summary) if result_summary is not None else None, service_request_id, retrieval_id, Jsonb(evidence_ids or []), error_code))

async def fail_run(run_id: UUID, token: UUID, code: str, reason: str | None=None) -> bool:
    try:
        return await _fail_run(run_id, token, code, reason)
    except PersistenceOutcomeUnknown as exc:
        operation = OperationIdentity('fail', task_ref(run_id), run_id, token)
        resolved = await verify_unknown(exc, operation, lambda: reconcile_operation(operation, {'code': code}))
        if resolved.state == ResolutionState.APPLIED:
            return True
        raise


@retry_transaction(profile='control')
async def _fail_run(run_id, token, code, reason):
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        result = await store.settle_failure(conn, task_ref(run_id), token, code, reason=reason)
        if result.accepted and result.status in TERMINAL:
            await interrupt_calls(conn, run_id)
        return result.accepted

async def publish_result(run_id: UUID, token: UUID, draft: dict, evidence: dict[str, dict], partial: bool, termination_reason: str, *, result_id: UUID | None=None) -> Applied:
    from agent_service.acceptance import verify_projection
    verify_projection(draft)
    result_id = result_id or uuid4()
    try:
        return await _publish_result(run_id, token, draft, evidence, partial, termination_reason, result_id=result_id)
    except PersistenceOutcomeUnknown as exc:
        operation = OperationIdentity('publish', task_ref(run_id), result_id, token)
        resolved = await verify_unknown(exc, operation, lambda: reconcile_operation(operation, {'draft': draft.draft, 'partial': partial}))
        if resolved.state == ResolutionState.APPLIED:
            return AlreadyApplied(resolved.value)
        raise


@retry_transaction
async def _publish_result(run_id: UUID, token: UUID, draft: dict, evidence: dict[str, dict], partial: bool, termination_reason: str, *, result_id: UUID) -> Applied:
    from agent_service.acceptance import verify_projection
    verify_projection(draft)
    checked = draft
    draft = checked.draft
    cited_ids = []
    for claim in draft['claims']:
        if claim['support'] == 'evidence' and (not claim['evidence_ids']):
            raise ValueError('supported claim has no evidence')
        if claim['support'] == 'unverified' and (not claim.get('reason')):
            raise ValueError('unverified claim requires a reason')
        for evidence_id in claim['evidence_ids']:
            if evidence_id not in evidence:
                raise ValueError('citation was not received by this run')
            cited_ids.append(evidence_id)
    cited_ids = list(dict.fromkeys(cited_ids))
    declared = [item['evidence_id'] for item in draft['citations']]
    if set(declared) != set(cited_ids) or len(declared) != len(set(declared)):
        raise ValueError('citation list does not match claim evidence')
    result_id = result_id or uuid4()
    from execution.persistence import resources,deadline
    from execution.scope import work_owner
    from execution.tokens import canonical
    proof_data=await resources().io.run(lambda value:canonical(value).encode(),checked.proof,
        owner=work_owner(),deadline=deadline(30))
    async with connect('AGENT_DATABASE_URL') as conn:
        state=await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE',(run_id,TaskKind.AGENT))).fetchone()
        existing=await (await conn.execute('SELECT * FROM agent.run_results WHERE run_id=%s',(run_id,))).fetchone()
        if existing:
            from agent_service.runs import load_result
            actual=await load_result(conn,run_id)
            expected={'answer':draft['answer'],'claims':[dict(c) for c in draft['claims']],'citations':[{k:c.get(k) for k in ('evidence_id','document_id','document_version_id','title','content','source_locator')} for c in draft['citations']]}
            expected['notices']=draft.get('notices',[])
            expected['acceptance']=draft.get('acceptance')
            # Normalize absent optional reason fields and citation ordering.
            for c in expected['claims']: c.setdefault('reason',None)
            expected['citations']=sorted(expected['citations'],key=lambda c:c['evidence_id'])
            if state['status'] != ('partial' if partial else 'completed') or actual != expected:
                raise ValueError('Result publication conflict')
            return AlreadyApplied(existing['id'])
        await store.assert_active(conn, task_ref(run_id), token)
        from agent_service.storage.acceptance import check_publication
        await check_publication(conn,run_id,checked,evidence,partial,proof_data=proof_data)
        await conn.execute('INSERT INTO agent.run_results(id,run_id,answer,notices,acceptance) VALUES (%s,%s,%s,%s,%s)', (result_id, run_id, draft['answer'], Jsonb(draft.get('notices', [])),Jsonb(draft['acceptance'])))
        for evidence_id in cited_ids:
            item = evidence[evidence_id]
            await conn.execute('INSERT INTO agent.evidence_snapshots(result_id,evidence_id,document_id,document_version_id,title,content,source_locator)\n                VALUES (%s,%s,%s,%s,%s,%s,%s)', (result_id, evidence_id, item.get('document_id'), item.get('document_version_id'), item['title'], item['content'], Jsonb(item['source_locator'])))
        for ordinal, claim in enumerate(draft['claims'], 1):
            claim_id = uuid4()
            await conn.execute('INSERT INTO agent.result_claims(id,result_id,ordinal,text,support,reason)\n                VALUES (%s,%s,%s,%s,%s,%s)', (claim_id, result_id, ordinal, claim['text'], claim['support'], claim.get('reason')))
            for evidence_id in claim['evidence_ids']:
                await conn.execute('INSERT INTO agent.claim_evidence(claim_id,result_id,evidence_id) VALUES (%s,%s,%s)', (claim_id, result_id, evidence_id))
        from execution.inputs import enqueue_memory
        turn = await (await conn.execute('SELECT id FROM agent.conversation_turns WHERE run_id=%s', (run_id,))).fetchone()
        if turn:
            await enqueue_memory(conn, turn['id'], 'summary')
        await store.complete(conn, task_ref(run_id), token, status='partial' if partial else 'completed', reason=termination_reason)
    return Applied(result_id)


@retry_transaction(profile='control')
async def reconcile_operation(operation: OperationIdentity, expected: dict) -> Resolution:
    """Query postconditions under the same task lock used by all writers.

    This is a control operation. A result is an acknowledgement, not authority
    to send an external request; no persistent state is modified here.
    """
    if operation.task.kind != TaskKind.AGENT:
        raise ValueError('Operation is not an Agent operation')
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE',
                                       (operation.task.id, TaskKind.AGENT))).fetchone()
        observed = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        active = store.authority_refusal(operation.task, operation.token, row, observed) is None
        if row is None:
            return Resolution(ResolutionState.REJECTED)
        if operation.kind.startswith(('reserve_', 'settle_')):
            kind = operation.kind.split('_', 1)[1]
            if kind not in ('model', 'tool'):
                raise ValueError('Unknown call operation')
            call = await (await conn.execute(f'SELECT * FROM agent.{kind}_calls WHERE id=%s', (operation.id,))).fetchone()
            if call is None:
                return Resolution(ResolutionState.NOT_APPLIED if active else ResolutionState.REJECTED)
            if call['run_id'] != operation.task.id or call['attempt_token'] != operation.token:
                return Resolution(ResolutionState.REJECTED)
            if operation.kind.startswith('settle_') and call['status'] == 'started':
                return Resolution(ResolutionState.NOT_APPLIED if active else ResolutionState.REJECTED)
            if not all(call.get(key) == value for key, value in expected.items()):
                return Resolution(ResolutionState.REJECTED)
            return Resolution(ResolutionState.RECORDED if operation.kind.startswith('reserve_') else ResolutionState.APPLIED, call['id'])
        if operation.kind == 'publish':
            result = await (await conn.execute('SELECT id FROM agent.run_results WHERE run_id=%s', (operation.task.id,))).fetchone()
            if result is None:
                return Resolution(ResolutionState.NOT_APPLIED if active else ResolutionState.REJECTED)
            from agent_service.storage.runs import load_result
            actual = await load_result(conn, operation.task.id)
            draft = expected['draft']
            wanted = {'acceptance': draft.get('acceptance'), 'answer': draft['answer'], 'claims': [dict(c) for c in draft['claims']],
                      'notices': draft.get('notices', []), 'citations': [{k: c.get(k) for k in
                        ('evidence_id','document_id','document_version_id','title','content','source_locator')} for c in draft['citations']]}
            for claim in wanted['claims']: claim.setdefault('reason', None)
            wanted['citations'].sort(key=lambda c: c['evidence_id'])
            if row['status'] != ('partial' if expected['partial'] else 'completed') or actual != wanted:
                return Resolution(ResolutionState.REJECTED)
            return Resolution(ResolutionState.APPLIED, result['id'])
        if operation.kind == 'fail':
            if row['status'] == 'cancelled' or row['status'] == 'failed' and row['error_code'] in (expected['code'], 'RUN_TIMEOUT'):
                return Resolution(ResolutionState.APPLIED, operation.id)
            return Resolution(ResolutionState.NOT_APPLIED if active else ResolutionState.REJECTED)
        raise ValueError('Unknown persistent operation')
