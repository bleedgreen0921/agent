from tests.acceptance_fixtures import fixture_publish_result
from tests.native_async import native, native_runnable
"""Pure execution rules and real-transaction fencing at every persistence boundary."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg
from psycopg.types.json import Jsonb
from execution.contracts import TaskRef, TaskKind, LeaseSnapshot, ExecutionContext, ToolOutcome, WorkHandle, bounded_timeout, public_status, freeze
from execution import store
from execution.scope import attempt, call_timeout
from db.connection import connect
from tests.helpers import insert_run
from agent_service import runtime
from agent_service.tooling import RunExecutionState, merge_outcome, load_providers
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)

def test_authority_priority_and_strict_expiry():
    task = TaskRef(uuid4(), TaskKind.AGENT)
    token = uuid4()
    from dataclasses import replace
    base = LeaseSnapshot(task, token, 'running', NOW + timedelta(seconds=30), NOW + timedelta(seconds=300))
    assert base.refusal(task, token, NOW) is None
    assert replace(base, leased_until=NOW).refusal(task, token, NOW) == 'lease_expired'
    assert replace(base, deadline_at=NOW).refusal(task, token, NOW) == 'deadline'
    assert replace(base, deadline_at=NOW, stop_requested_at=NOW).refusal(task, token, NOW) == 'user_cancel'
    assert replace(base, stop_requested_at=NOW).refusal(task, uuid4(), NOW) == 'takeover'
    assert replace(base, status='completed').refusal(task, uuid4(), NOW) == 'terminal'

def test_contract_timeout_mappings_and_capacity():
    assert bounded_timeout(120, NOW + timedelta(seconds=2), NOW) == 2
    with pytest.raises(PermissionError):
        bounded_timeout(120, NOW, NOW)
    assert public_status(TaskKind.AGENT, 'stopping') == 'cancelling'
    assert public_status(TaskKind.MEMORY, 'queued') == 'pending'
    assert public_status(TaskKind.MEMORY, 'completed') == 'done'
    assert public_status(TaskKind.DOCUMENT, 'queued', 1) == 'retry'
    handle = WorkHandle(TaskRef(uuid4(), TaskKind.AGENT))
    handle.request_stop()
    assert handle.occupies_capacity
    handle.physically_done = True
    assert not handle.occupies_capacity

def test_context_and_outcome_copy_json_and_reject_unsafe_metadata():
    data = {'experiment': {'entries': [{'value': 1}]}}
    context = ExecutionContext(TaskRef(uuid4(), TaskKind.AGENT), uuid4(), NOW, inputs=data)
    data['experiment']['entries'][0]['value'] = 2
    assert context.inputs['experiment']['entries'][0]['value'] == 1
    with pytest.raises(FrozenInstanceError):
        context.call_id = uuid4()
    with pytest.raises(TypeError):
        context.inputs['x'] = 1
    with pytest.raises(ValueError):
        ToolOutcome('x', metadata={'lease_token': str(uuid4())})
    with pytest.raises(ValueError):
        ToolOutcome.from_artifact({'contract_version': 0})
    with pytest.raises(ValueError):
        ToolOutcome('x', metadata={'status': 'started'})

def evidence(content='same'):
    return {'evidence_id': 'ev_shared', 'document_id': 'doc_a', 'document_version_id': 'ver_a', 'title': 'A', 'content': content, 'source_locator': {'kind': 'txt', 'line_start': 1, 'line_end': 1}}

def test_merge_rejects_evidence_conflict_and_keeps_first_notice():
    state = RunExecutionState(uuid4(), uuid4())
    merge_outcome(state, ToolOutcome('x', (evidence(),), ({'code': 'N', 'message': 'first'},)))
    merge_outcome(state, ToolOutcome('x', ({**evidence(), 'rank': 2},), ({'code': 'N', 'message': 'second'},)))
    assert len(state.evidence) == 1 and state.notices == [{'code': 'N', 'message': 'first'}]
    with pytest.raises(ValueError, match='Conflicting'):
        merge_outcome(state, ToolOutcome('x', (evidence('changed'),), ({'code': 'BAD', 'message': 'bad'},)))
    assert state.evidence['ev_shared']['content'] == 'same' and len(state.notices) == 1
    empty = RunExecutionState(uuid4(), uuid4())
    with pytest.raises(ValueError, match='Conflicting'):
        merge_outcome(empty, ToolOutcome('x', (evidence(), evidence('changed'))))
    assert empty.evidence == {} and empty.notices == []
DB = pytest.mark.skipif(not os.environ.get('MIGRATION_DATABASE_URL'), reason='isolated PostgreSQL not configured')

@DB
@pytest.mark.parametrize('kind', list(TaskKind))
@pytest.mark.parametrize('revoked', ['lease', 'deadline', 'token', 'stopping'])
async def test_every_task_rejects_old_execution_and_expired_renewal(kind, revoked):
    task = TaskRef(uuid4(), kind)
    with connect(store.ENV[kind]) as conn:
        await native(store.create, conn, task)
        row = await native(store.claim, conn, kind, lease_seconds=60)
    token = row['lease_token']
    with connect(store.ENV[kind]) as conn:
        if revoked == 'token':
            conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (uuid4(), task.id))
        elif revoked == 'stopping':
            conn.execute("UPDATE execution.tasks SET status='stopping',stop_requested_at=clock_timestamp(),stop_reason='user_cancel' WHERE id=%s", (task.id,))
        else:
            conn.execute(f"UPDATE execution.tasks SET {('leased_until' if revoked == 'lease' else 'execution_deadline_at')}=clock_timestamp()-interval '1 second' WHERE id=%s", (task.id,))
    assert not await native(store.renew, task, token, 60)
    with pytest.raises(PermissionError):
        await native(store.check, task, token)
    with pytest.raises(PermissionError):
        with connect(store.ENV[kind]) as conn:
            await native(store.complete, conn, task, token)
    with attempt(task, token), pytest.raises(PermissionError):
        await native(call_timeout, 120)

@DB
@pytest.mark.parametrize('cause', ['lease', 'deadline', 'cancel', 'takeover'])
async def test_final_answer_never_publishes_after_revocation(cause):
    rid = insert_run()
    claimed = await native(runtime.claim_run)
    token = claimed['lease_token']
    with connect('AGENT_DATABASE_URL') as conn:
        if cause == 'takeover':
            conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (uuid4(), rid))
        elif cause == 'cancel':
            await native(store.stop, conn, TaskRef(rid, TaskKind.AGENT))
        else:
            conn.execute(f"UPDATE execution.tasks SET {('leased_until' if cause == 'lease' else 'execution_deadline_at')}=clock_timestamp()-interval '1 second' WHERE id=%s", (rid,))
    draft = {'answer': 'late', 'claims': [], 'citations': [], 'notices': []}
    with pytest.raises(PermissionError):
        await native(fixture_publish_result, rid, token, draft, {}, False, 'done')
    with connect('AGENT_DATABASE_URL') as conn:
        assert not conn.execute('SELECT 1 FROM agent.run_results').fetchone()
    with pytest.raises(PermissionError):
        await native(runtime.reserve_model, rid, token, 'react', 'mock')

@DB
async def test_final_deadline_recheck_rolls_back_inserted_result(monkeypatch):
    from contextlib import contextmanager
    rid = insert_run()
    claimed = await native(runtime.claim_run)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()+interval '0.25 second' WHERE id=%s", (rid,))
    from agent_service.storage import runtime as runtime_store
    original = runtime_store.connect

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def slow_connect(*args, **kwargs):
        async with original(*args, **kwargs) as conn:

            class Proxy:

                async def execute(self, query, *params):
                    result = await conn.execute(query, *params)
                    if 'INSERT INTO agent.run_results' in query:
                        await conn.execute('SELECT pg_sleep(0.3)')
                    return result
            yield Proxy()
    monkeypatch.setattr(runtime_store, 'connect', slow_connect)
    with pytest.raises(PermissionError):
        await native(fixture_publish_result, rid, claimed['lease_token'], {'answer': 'late', 'claims': [], 'citations': [], 'notices': []}, {}, False, 'done')
    with connect('AGENT_DATABASE_URL') as conn:
        assert not conn.execute('SELECT 1 FROM agent.run_results').fetchone()

@DB
@pytest.mark.parametrize('write', ['checkpoint', 'blob', 'intermediate'])
async def test_checkpoint_actual_connection_rolls_back_on_expiry(write):
    from agent_service.checkpoints import protected_saver, with_agent_search_path
    rid = insert_run()
    claimed = await native(runtime.claim_run)
    task = TaskRef(rid, TaskKind.AGENT)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()+interval '0.25 second' WHERE id=%s", (rid,))
    dsn = with_agent_search_path(os.environ['AGENT_DATABASE_URL'], runtime=True)
    async with protected_saver(dsn, task, claimed['lease_token']) as saver:
        with pytest.raises(PermissionError):
            async with saver._cursor(pipeline=True) as cur:
                if write == 'checkpoint':
                    await cur.execute("INSERT INTO checkpoints(thread_id,checkpoint_ns,checkpoint_id,checkpoint,metadata) VALUES (%s,'','probe','{}','{}')", (str(rid),))
                elif write == 'blob':
                    await cur.execute("INSERT INTO checkpoint_blobs(thread_id,checkpoint_ns,channel,version,type,blob) VALUES (%s,'','probe','1','bytes',%s)", (str(rid), b'x'))
                else:
                    await cur.execute("INSERT INTO checkpoint_writes(thread_id,checkpoint_ns,checkpoint_id,task_id,idx,channel,type,blob,task_path) VALUES (%s,'','probe','probe',0,'probe','bytes',%s,'')", (str(rid), b'x'))
                await cur.execute('SELECT pg_sleep(0.3)')
    with connect('AGENT_DATABASE_URL') as conn:
        for table in ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes'):
            assert not conn.execute(f'SELECT 1 FROM agent.{table} WHERE thread_id=%s', (str(rid),)).fetchone()

@DB
async def test_old_checkpoint_put_and_intermediate_calls_are_fenced():
    from agent_service.checkpoints import protected_saver, with_agent_search_path
    from langgraph.checkpoint.base import empty_checkpoint
    rid = insert_run()
    claimed = await native(runtime.claim_run)
    task = TaskRef(rid, TaskKind.AGENT)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (uuid4(), rid))
    config = {'configurable': {'thread_id': str(rid), 'checkpoint_ns': '', 'checkpoint_id': 'probe'}}
    async with protected_saver(with_agent_search_path(os.environ['AGENT_DATABASE_URL']), task, claimed['lease_token']) as saver:
        with pytest.raises(PermissionError):
            await native(saver.aput, config, empty_checkpoint(), {}, {})
        with pytest.raises(PermissionError):
            await native(saver.aput_writes, config, [('messages', 'late')], 'probe')

@DB
async def test_task_types_are_hidden_and_cannot_be_changed_by_other_service():
    ref = TaskRef(uuid4(), TaskKind.DOCUMENT)
    with connect('RAG_DATABASE_URL') as conn:
        await native(store.create, conn, ref)
    with connect('AGENT_DATABASE_URL') as conn:
        assert not conn.execute('SELECT 1 FROM execution.tasks WHERE id=%s', (ref.id,)).fetchone()
        assert conn.execute("UPDATE execution.tasks SET status='cancelled' WHERE id=%s RETURNING id", (ref.id,)).fetchone() is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect('RAG_DATABASE_URL') as conn:
            conn.execute("UPDATE execution.tasks SET kind='agent_run' WHERE id=%s", (ref.id,))

@DB
async def test_worker_exit_safe_release_unknown_result_and_takeover():
    rid = insert_run()
    first = await native(runtime.claim_run)
    await native(runtime.attempt_exited, rid, first['lease_token'])
    from contracts.v1 import RunResponse
    from agent_service.runs import get_run
    assert RunResponse.model_validate(await native(get_run, rid)).error is None
    second = await native(runtime.claim_run)
    assert second['lease_token'] != first['lease_token']
    await native(runtime.attempt_exited, rid, first['lease_token'])
    assert RunResponse.model_validate(await native(get_run, rid)).error is None
    assert await native(store.check, TaskRef(rid, TaskKind.AGENT), second['lease_token'])
    await native(runtime.reserve_model, rid, second['lease_token'], 'react', 'mock')
    await native(runtime.attempt_exited, rid, second['lease_token'])
    with connect('AGENT_DATABASE_URL') as conn:
        row = conn.execute('SELECT status,error_code,stop_requested_at FROM execution.tasks WHERE id=%s', (rid,)).fetchone()
    assert row == {'status': 'failed', 'error_code': 'INTERRUPTED_UNKNOWN', 'stop_requested_at': None}

@DB
async def test_tool_late_outcome_is_settled_but_never_merged():
    from agent_service.tooling import budgeted_tool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime
    from langchain_core.messages import ToolMessage
    rid = insert_run()
    claim = await native(runtime.claim_run)
    state = RunExecutionState(rid, claim['lease_token'])
    request = ToolCallRequest(tool_call={'name': 'probe', 'args': {}, 'id': 'call', 'type': 'tool_call'}, tool=None, state={}, runtime=ToolRuntime(context=state, state={}, config={}, stream_writer=lambda _: None, tool_call_id='call', store=None))

    async def handler(req):
        assert isinstance(req.runtime.context, ExecutionContext)
        with connect('AGENT_DATABASE_URL') as conn:
            await native(store.stop, conn, TaskRef(rid, TaskKind.AGENT))
        artifact = ToolOutcome('late', (evidence(),), ({'code': 'LATE', 'message': 'late'},)).to_artifact()
        return ToolMessage(content='late', artifact=artifact, tool_call_id='call')
    with pytest.raises(PermissionError):
        await native(budgeted_tool.awrap_tool_call, request, handler)
    assert state.evidence == {} and state.notices == []
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s', (rid,)).fetchone()['tool_calls_used'] == 1
        assert conn.execute('SELECT status FROM agent.tool_calls WHERE run_id=%s', (rid,)).fetchone()['status'] == 'started'

def test_registry_does_not_release_logical_cancellation():
    from execution.resources import WorkRegistry
    registry = WorkRegistry()
    handle = WorkHandle(TaskRef(uuid4(), TaskKind.AGENT))
    key = registry.register(handle)
    handle.request_stop()
    assert registry.occupied == 1
    with pytest.raises(RuntimeError):
        registry.release(key)
    handle.physically_done = True
    registry.release(key)
    assert registry.occupied == 0

@DB
async def test_failed_memory_renewal_signal_blocks_even_a_live_lease():
    import threading
    task = TaskRef(uuid4(), TaskKind.MEMORY)
    with connect('AGENT_DATABASE_URL') as conn:
        await native(store.create, conn, task)
        row = await native(store.claim, conn, TaskKind.MEMORY, lease_seconds=300)
    stop = threading.Event()
    stop.set()
    with attempt(task, row['lease_token'], stop):
        with pytest.raises(PermissionError):
            await native(store.check, task, row['lease_token'])
        with pytest.raises(PermissionError):
            with connect('AGENT_DATABASE_URL') as conn:
                await native(store.complete, conn, task, row['lease_token'])

@DB
async def test_external_calls_use_remaining_deadline_and_new_token_wins_cancel():
    task = TaskRef(uuid4(), TaskKind.AGENT)
    with connect('AGENT_DATABASE_URL') as conn:
        await native(store.create, conn, task)
        row = await native(store.claim, conn, TaskKind.AGENT, lease_seconds=60)
        conn.execute("UPDATE execution.tasks SET execution_deadline_at=clock_timestamp()+interval '2 seconds' WHERE id=%s", (task.id,))
    with attempt(task, row['lease_token']):
        assert 0 < await native(call_timeout, 120) <= 2
    new_token = uuid4()
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (new_token, task.id))
        await native(store.stop, conn, task)
        assert not (await native(store.settle_failure, conn, task, row['lease_token'], 'MODEL_CALL_FAILED', worker_exit=True)).accepted
        assert (await native(store.settle_failure, conn, task, new_token, 'MODEL_CALL_FAILED', worker_exit=True)).status == 'cancelled'

@DB
async def test_rag_retry_has_delay_and_fixed_total_deadline():
    task = TaskRef(uuid4(), TaskKind.DOCUMENT)
    with connect('RAG_DATABASE_URL') as conn:
        await native(store.create, conn, task)
        first = await native(store.claim, conn, TaskKind.DOCUMENT, lease_seconds=60)
        result = await native(store.settle_failure, conn, task, first['lease_token'], 'EMBEDDING_UNAVAILABLE', retryable=True)
        assert result.status == 'queued'
        row = conn.execute('SELECT *,clock_timestamp() AS now FROM execution.tasks WHERE id=%s', (task.id,)).fetchone()
        assert 9 < (row['available_at'] - row['now']).total_seconds() <= 10
        assert await native(store.claim, conn, TaskKind.DOCUMENT, lease_seconds=60) is None
        conn.execute('UPDATE execution.tasks SET available_at=clock_timestamp() WHERE id=%s', (task.id,))
        second = await native(store.claim, conn, TaskKind.DOCUMENT, lease_seconds=60)
    assert second['execution_deadline_at'] == first['execution_deadline_at'] and second['attempts'] == 2

@DB
async def test_heartbeat_waiting_for_a_lock_cannot_resurrect_expired_lease():
    import threading
    task = TaskRef(uuid4(), TaskKind.AGENT)
    with connect('AGENT_DATABASE_URL') as conn:
        await native(store.create, conn, task)
        row = await native(store.claim, conn, TaskKind.AGENT, lease_seconds=60)
        conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()+interval '0.5 second' WHERE id=%s", (task.id,))
    import asyncio
    from execution.persistence import resources
    entered=asyncio.Event()
    async def renew():
        entered.set()
        return await native(store.renew,task,row['lease_token'],60)
    with connect('AGENT_DATABASE_URL') as holder:
        holder.execute('SELECT id FROM execution.tasks WHERE id=%s FOR UPDATE',(task.id,))
        renewal=asyncio.create_task(renew())
        await entered.wait()
        await asyncio.sleep(.6)
    assert await renewal is False

@DB
async def test_authority_expiring_during_tool_merge_leaves_state_unchanged(monkeypatch):
    from agent_service import tooling
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime
    from langchain_core.messages import ToolMessage
    rid = insert_run()
    claim = await native(runtime.claim_run)
    state = RunExecutionState(rid, claim['lease_token'])
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()+interval '1 second' WHERE id=%s", (rid,))
    original = tooling.merge_collections

    def delayed_merge(*args):
        original(*args)
        time.sleep(1.1)
    monkeypatch.setattr(tooling, 'merge_collections', delayed_merge)
    request = ToolCallRequest(tool_call={'name': 'probe', 'args': {}, 'id': 'call', 'type': 'tool_call'}, tool=None, state={}, runtime=ToolRuntime(context=state, state={}, config={}, stream_writer=lambda _: None, tool_call_id='call', store=None))

    async def handler(_):
        artifact = ToolOutcome('observation', (evidence(),), ({'code': 'N', 'message': 'notice'},)).to_artifact()
        return ToolMessage(content='observation', artifact=artifact, tool_call_id='call')
    with pytest.raises(PermissionError):
        await native(tooling.budgeted_tool.awrap_tool_call, request, handler)
    assert state.evidence == {} and state.notices == []
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT status FROM agent.tool_calls WHERE run_id=%s', (rid,)).fetchone()['status'] == 'succeeded'


@DB
@pytest.mark.parametrize('operation', ['renew', 'publish'])
@pytest.mark.parametrize('expiry', ['lease', 'deadline'])
async def test_locked_task_expiry_rejects_same_token_without_takeover(monkeypatch, operation, expiry):
    """Observe a real PostgreSQL lock wait, then release only after DB-time expiry."""
    import asyncio
    from contextlib import asynccontextmanager
    from execution.persistence import transaction
    from agent_service.storage import runtime as runtime_store

    rid = insert_run()
    claimed = await native(runtime.claim_run)
    token = claimed['lease_token']
    task = TaskRef(rid, TaskKind.AGENT)
    target = store if operation == 'renew' else runtime_store
    original = target.connect
    attempted = asyncio.Event()
    backend = []
    pending = None
    column = 'leased_until' if expiry == 'lease' else 'execution_deadline_at'
    async with transaction(readonly=True) as conn:
        baseline = await (await conn.execute('SELECT heartbeat_at,leased_until,execution_deadline_at FROM execution.tasks WHERE id=%s', (rid,))).fetchone()

    @asynccontextmanager
    async def traced(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            class Proxy:
                async def execute(self, query, *params):
                    if 'execution.tasks' in query and 'FOR UPDATE' in query:
                        backend[:] = [conn.info.backend_pid]
                        attempted.set()
                    return await conn.execute(query, *params)
            yield Proxy()

    async def candidate():
        if operation == 'renew':
            return await native(store.renew, task, token, 60)
        draft = {'answer': 'late', 'claims': [], 'citations': [], 'notices': []}
        return await native(fixture_publish_result, rid, token, draft, {}, False, 'done')

    monkeypatch.setattr(target, 'connect', traced)
    try:
        async with transaction() as holder:
            # Changing the deadline is still uncommitted. Candidate must block
            # on this row, while the original committed state remains running.
            row = await (await holder.execute(
                f"UPDATE execution.tasks SET {column}=clock_timestamp()+interval '2 seconds' WHERE id=%s RETURNING {column} AS expires",
                (rid,))).fetchone()
            expires = row['expires']
            pending = asyncio.create_task(candidate())
            async with asyncio.timeout(3):
                await attempted.wait()
                async with transaction(readonly=True) as monitor:
                    while True:
                        activity = await (await monitor.execute(
                            'SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked, (SELECT xact_start FROM pg_stat_activity WHERE pid=%s) AS xact_start',
                            (holder.info.backend_pid, backend[0], backend[0]))).fetchone()
                        if activity['blocked']:
                            assert activity['xact_start'] < expires
                            break
                        assert not pending.done(), 'Candidate never reached the lock wait'
                        await asyncio.sleep(0)
            # Wait for the actual server deadline, rather than guessing a sleep
            # duration on the client or relying on a sweeper/second executor.
            await holder.execute(
                'SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (%s::timestamptz-clock_timestamp())))::double precision)',
                (expires,))
            observed = await (await holder.execute(
                'SELECT clock_timestamp()>=%s AS expired,transaction_timestamp()<%s AS old_transaction_time',
                (expires, expires))).fetchone()
            assert observed['expired'] and observed['old_transaction_time']
            assert not pending.done()
        if operation == 'renew':
            assert await pending is False
        else:
            with pytest.raises(PermissionError):
                await pending
        async with transaction(readonly=True) as conn:
            state = await (await conn.execute(
                'SELECT status,lease_token,stop_requested_at,leased_until,execution_deadline_at,heartbeat_at FROM execution.tasks WHERE id=%s',
                (rid,))).fetchone()
            assert state['status'] == 'running' and state['lease_token'] == token
            assert state['stop_requested_at'] is None and state[column] == expires
            assert state['heartbeat_at'] == baseline['heartbeat_at']
            other = 'execution_deadline_at' if expiry == 'lease' else 'leased_until'
            assert state[other] == baseline[other]
            assert not await (await conn.execute('SELECT 1 FROM agent.run_results WHERE run_id=%s', (rid,))).fetchone()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@DB
@pytest.mark.parametrize('expiry', ['lease', 'deadline'])
async def test_publication_expiry_rolls_back_all_products_and_summary_task(monkeypatch, expiry):
    from contextlib import asynccontextmanager
    from execution.persistence import transaction
    from agent_service.storage import runtime as runtime_store

    rid = insert_run()
    claimed = await native(runtime.claim_run)
    token = claimed['lease_token']
    conversation_id, turn_id = uuid4(), uuid4()
    async with transaction() as conn:
        await conn.execute('INSERT INTO agent.conversations(id) VALUES (%s)', (conversation_id,))
        await conn.execute('''INSERT INTO agent.conversation_turns(id,conversation_id,ordinal,run_id,user_text,idempotency_key,request_digest)
            VALUES (%s,%s,1,%s,'test','expiry',%s)''', (turn_id, conversation_id, rid, b'x' * 32))
    column = 'leased_until' if expiry == 'lease' else 'execution_deadline_at'
    original = runtime_store.connect
    prepared = []

    @asynccontextmanager
    async def expires_after_products(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            class Proxy:
                async def execute(self, query, *params):
                    result = await conn.execute(query, *params)
                    if 'INSERT INTO agent.memory_jobs' in query:
                        # Products really exist in this transaction before the
                        # final guard. Expire authority without any takeover.
                        for table in ('run_results', 'result_claims', 'evidence_snapshots', 'claim_evidence', 'memory_jobs'):
                            assert await (await conn.execute(f'SELECT 1 FROM agent.{table}')).fetchone()
                        prepared.append(True)
                        await conn.execute(f"UPDATE execution.tasks SET {column}=clock_timestamp()-interval '1 second' WHERE id=%s", (rid,))
                    return result
            yield Proxy()

    monkeypatch.setattr(runtime_store, 'connect', expires_after_products)
    item = evidence()
    draft = {'answer': 'late', 'claims': [{'text': 'same', 'support': 'evidence', 'evidence_ids': [item['evidence_id']]}],
             'citations': [item], 'notices': []}
    with pytest.raises(PermissionError):
        await native(fixture_publish_result, rid, token, draft, {item['evidence_id']: item}, False, 'done')
    assert prepared == [True]
    async with transaction(readonly=True) as conn:
        for table in ('run_results', 'result_claims', 'evidence_snapshots', 'claim_evidence', 'memory_jobs'):
            assert not await (await conn.execute(f'SELECT 1 FROM agent.{table}')).fetchone()
        assert not await (await conn.execute("SELECT 1 FROM execution.tasks WHERE kind='memory_job'")).fetchone()
        state = await (await conn.execute('SELECT status,lease_token FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
        assert state == {'status': 'running', 'lease_token': token}
