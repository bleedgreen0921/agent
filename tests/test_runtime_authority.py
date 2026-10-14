"""R01: real task locks, call settlement, receipts, and control priorities."""
import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from agent_service import checkpoints, runtime
from agent_service.storage import runtime as runtime_store, receipts
from execution import store, worker
from execution.contracts import TaskRef, TaskKind
from execution.infra.types import WorkOwner
from execution.persistence import transaction, resources, Applied, AlreadyApplied, Rejected
from tests.helpers import insert_run

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


async def _run(kind='model', settled=False):
    rid = insert_run()
    claim = await runtime.claim_run()
    token = claim['lease_token']
    call = await runtime.reserve_model(rid, token, 'react', 'mock') if kind == 'model' else await runtime.reserve_tool(rid, token, 'probe', {})
    if settled:
        assert isinstance(await runtime.settle_model(rid, token, call, 'succeeded'), Applied)
    return rid, token, call


def _checkpoint(rid, call, identifier='r01'):
    cp = empty_checkpoint()
    cp['id'] = identifier
    cp['channel_values'] = {'messages': [AIMessage(content='durable output', additional_kwargs={receipts.MARKER: receipts.marker('model', call)})]}
    cp['channel_versions'] = {'messages': identifier}
    return cp, {'configurable': {'thread_id': str(rid), 'checkpoint_ns': ''}}


async def _revoke(conn, rid, cause):
    if cause == 'cancel':
        await store.stop(conn, TaskRef(rid, TaskKind.AGENT))
    elif cause == 'takeover':
        await conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (uuid4(), rid))
    else:
        column = 'leased_until' if cause == 'lease' else 'execution_deadline_at'
        await conn.execute(f"UPDATE execution.tasks SET {column}=clock_timestamp()-interval '1 second' WHERE id=%s", (rid,))


@pytest.mark.parametrize('operation', ['model', 'tool', 'checkpoint', 'pending', 'fail'])
@pytest.mark.parametrize('cause', ['cancel', 'takeover', 'lease', 'deadline'])
async def test_task_lock_serializes_settlement_checkpoint_and_failure(monkeypatch, operation, cause):
    rid, token, call = await _run('tool' if operation == 'tool' else 'model', settled=operation in {'checkpoint', 'pending'})
    cp, config = _checkpoint(rid, call)
    saver = checkpoints.ProtectedSaver(TaskRef(rid, TaskKind.AGENT), token)
    if operation == 'pending':
        base = empty_checkpoint()
        base['id'] = 'base'
        config = await saver.aput(config, base, {}, {})
    target = checkpoints if operation in {'checkpoint', 'pending'} else runtime_store
    method = 'transaction' if target is checkpoints else 'connect'
    original = getattr(target, method)
    attempted, backend = asyncio.Event(), []

    @asynccontextmanager
    async def traced(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            class Proxy:
                def __getattr__(self, name): return getattr(conn, name)
                async def execute(self, query, *params):
                    if 'execution.tasks' in query and 'FOR UPDATE' in query:
                        backend[:] = [conn.info.backend_pid]
                        attempted.set()
                    return await conn.execute(query, *params)
            yield Proxy()
    monkeypatch.setattr(target, method, traced)

    async def candidate():
        if operation == 'model': return await runtime.settle_model(rid, token, call, 'succeeded')
        if operation == 'tool': return await runtime.settle_tool(rid, token, call, 'succeeded')
        if operation == 'fail': return await runtime.fail_run(rid, token, 'MODEL_CALL_FAILED')
        if operation == 'checkpoint': return await saver.aput(config, cp, {}, cp['channel_versions'])
        return await saver.aput_writes(config, [('messages', cp['channel_values']['messages'])], 'node')

    pending = None
    try:
        async with transaction() as holder:
            await holder.execute('SELECT id FROM execution.tasks WHERE id=%s FOR UPDATE', (rid,))
            pending = asyncio.create_task(candidate())
            async with asyncio.timeout(5):
                await attempted.wait()
                async with transaction(readonly=True) as monitor:
                    while True:
                        blocked = await (await monitor.execute('SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked',
                            (holder.info.backend_pid, backend[0]))).fetchone()
                        if blocked['blocked']: break
                        assert not pending.done(), 'Operation did not wait on the task row'
                        await asyncio.sleep(0)
            if cause in {'lease', 'deadline'}:
                column = 'leased_until' if cause == 'lease' else 'execution_deadline_at'
                expires = (await (await holder.execute(f"UPDATE execution.tasks SET {column}=clock_timestamp()+interval '0.2 seconds' WHERE id=%s RETURNING {column} AS at", (rid,))).fetchone())['at']
                await holder.execute('SELECT pg_sleep_until(%s)', (expires,))
                times = await (await holder.execute('SELECT clock_timestamp() AS at, (SELECT xact_start FROM pg_stat_activity WHERE pid=%s) AS tx', (backend[0],))).fetchone()
                assert times['tx'] < expires <= times['at']
            else:
                await _revoke(holder, rid, cause)
            expected = await (await holder.execute('SELECT status,lease_token FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
        async with asyncio.timeout(5):
            if operation in {'checkpoint', 'pending'}:
                with pytest.raises(PermissionError): await pending
            elif operation == 'fail':
                assert await pending is (cause in {'cancel', 'deadline'})
            else:
                assert isinstance(await pending, Rejected)
        async with transaction(readonly=True) as conn:
            current = await (await conn.execute('SELECT status,lease_token,error_code FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
            if operation == 'fail' and cause in {'cancel', 'deadline'}:
                assert current == {'status': 'cancelled' if cause == 'cancel' else 'failed', 'lease_token': None,
                                   'error_code': None if cause == 'cancel' else 'RUN_TIMEOUT'}
            else:
                assert {k: current[k] for k in expected} == expected
            table = 'tool_calls' if operation == 'tool' else 'model_calls'
            state = await (await conn.execute(f'SELECT status,checkpointed,finished_at FROM agent.{table} WHERE id=%s', (call,))).fetchone()
            assert state['status'] == ('interrupted_unknown' if operation == 'fail' and cause in {'cancel', 'deadline'} else 'succeeded' if operation in {'checkpoint', 'pending'} else 'started')
            assert state['checkpointed'] is False
            if state['status'] == 'started': assert state['finished_at'] is None
            assert not await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
            assert not await (await conn.execute("SELECT 1 FROM agent.checkpoints WHERE thread_id=%s AND checkpoint_id='r01'", (str(rid),))).fetchone()
            assert not await (await conn.execute('SELECT 1 FROM agent.checkpoint_writes WHERE thread_id=%s', (str(rid),))).fetchone()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def _unconfirmed_output(monkeypatch):
    rid, token, call = await _run(settled=True)
    cp, config = _checkpoint(rid, call)
    saver = checkpoints.ProtectedSaver(TaskRef(rid, TaskKind.AGENT), token)
    original = checkpoints.confirm
    monkeypatch.setattr(checkpoints, 'confirm', lambda *args, **kwargs: None)
    try:
        await saver.aput(config, cp, {}, cp['channel_versions'])
    finally:
        monkeypatch.setattr(checkpoints, 'confirm', original)
    async with transaction(readonly=True) as conn:
        digest = (await (await conn.execute("""SELECT sha256(convert_to(c.checkpoint::text,'UTF8')||convert_to(b.type,'UTF8')||b.blob) AS digest
            FROM agent.checkpoints c JOIN agent.checkpoint_blobs b ON b.thread_id=c.thread_id AND b.checkpoint_ns=c.checkpoint_ns
            AND b.channel='messages' AND b.version=c.checkpoint->'channel_versions'->>'messages'
            WHERE c.thread_id=%s AND c.checkpoint_id='r01'""", (str(rid),))).fetchone())['digest']
    kwargs = dict(source='checkpoint', thread=str(rid), ns='', checkpoint_id='r01', channel='messages', digest=digest)
    return rid, token, call, kwargs


@pytest.mark.parametrize('cause', ['cancel', 'takeover', 'lease', 'deadline'])
async def test_confirmation_primitive_enforces_authority_without_saver(monkeypatch, cause):
    rid, token, call, kwargs = await _unconfirmed_output(monkeypatch)
    async with transaction() as conn: await _revoke(conn, rid, cause)
    with pytest.raises(PermissionError):
        async with transaction() as conn:
            await receipts.confirm(conn, rid, token, [('model', call)], **kwargs)
    async with transaction(readonly=True) as conn:
        assert not await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
        assert (await (await conn.execute('SELECT checkpointed FROM agent.model_calls WHERE id=%s', (call,))).fetchone())['checkpointed'] is False


@pytest.mark.parametrize('expiry', ['lease', 'deadline'])
async def test_confirmation_primitive_rolls_back_if_expired_after_writing_receipt(monkeypatch, expiry):
    rid, token, call, kwargs = await _unconfirmed_output(monkeypatch)
    with pytest.raises(PermissionError):
        async with transaction() as conn:
            class Proxy:
                async def execute(self, query, *params):
                    cursor = await conn.execute(query, *params)
                    if 'SET checkpointed=true' in query:
                        assert await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
                        assert (await (await conn.execute('SELECT checkpointed FROM agent.model_calls WHERE id=%s', (call,))).fetchone())['checkpointed']
                        await _revoke(conn, rid, expiry)
                    return cursor
            await receipts.confirm(Proxy(), rid, token, [('model', call)], **kwargs)
    async with transaction(readonly=True) as conn:
        assert not await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
        assert (await (await conn.execute('SELECT checkpointed FROM agent.model_calls WHERE id=%s', (call,))).fetchone())['checkpointed'] is False


@pytest.mark.parametrize('mode', ['checkpoint', 'pending'])
@pytest.mark.parametrize('expiry', ['lease', 'deadline'])
async def test_saver_final_check_rolls_back_graph_and_receipt_together(monkeypatch, mode, expiry):
    rid, token, call = await _run(settled=True)
    cp, config = _checkpoint(rid, call)
    saver = checkpoints.ProtectedSaver(TaskRef(rid, TaskKind.AGENT), token)
    if mode == 'pending':
        base = empty_checkpoint(); base['id'] = 'base'
        config = await saver.aput(config, base, {}, {})
    original = checkpoints.confirm
    async def expire_after_confirm(conn, *args, **kwargs):
        await original(conn, *args, **kwargs)
        assert await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
        await _revoke(conn, rid, expiry)
    monkeypatch.setattr(checkpoints, 'confirm', expire_after_confirm)
    with pytest.raises(PermissionError):
        if mode == 'checkpoint': await saver.aput(config, cp, {}, cp['channel_versions'])
        else: await saver.aput_writes(config, [('messages', cp['channel_values']['messages'])], 'node')
    async with transaction(readonly=True) as conn:
        assert not await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s', (rid,))).fetchone()
        assert (await (await conn.execute('SELECT checkpointed FROM agent.model_calls WHERE id=%s', (call,))).fetchone())['checkpointed'] is False
        assert not await (await conn.execute("SELECT 1 FROM agent.checkpoint_blobs WHERE thread_id=%s AND version='r01'", (str(rid),))).fetchone()
        assert not await (await conn.execute("SELECT 1 FROM agent.checkpoints WHERE thread_id=%s AND checkpoint_id='r01'", (str(rid),))).fetchone()
        assert not await (await conn.execute('SELECT 1 FROM agent.checkpoint_writes WHERE thread_id=%s', (str(rid),))).fetchone()


@pytest.mark.parametrize('cause, expected', [('normal','running'), ('lease','lost'), ('deadline','timeout'),
    ('cancel','cancel'), ('stopping','cancel'), ('queued','lost'), ('terminal','lost'), ('takeover','lost'), ('cancel_deadline','cancel')])
async def test_control_and_failure_priorities_share_contract(monkeypatch, cause, expected):
    rid, token, call = await _run()
    async with transaction() as conn:
        if cause in {'cancel', 'takeover', 'lease', 'deadline'}: await _revoke(conn, rid, cause)
        elif cause == 'cancel_deadline':
            await _revoke(conn, rid, 'cancel'); await _revoke(conn, rid, 'deadline')
        elif cause in {'stopping', 'queued'}:
            await conn.execute('UPDATE execution.tasks SET status=%s WHERE id=%s', (cause, rid))
        elif cause == 'terminal':
            await conn.execute("UPDATE execution.tasks SET status='failed' WHERE id=%s", (rid,))
    assert await runtime.control_state(rid, token) == expected
    task = TaskRef(rid, TaskKind.AGENT)
    if expected == 'running': assert await store.check(task, token)
    else:
        with pytest.raises(PermissionError): await store.check(task, token)
    stopped, settled = [], []
    monkeypatch.setattr(worker, 'stop', lambda *args: stopped.append(True))
    monkeypatch.setattr(worker, 'settle', lambda *args: settled.append(True))
    now = asyncio.get_running_loop().time()
    await worker.control(SimpleNamespace(owner=WorkOwner(task, token), last_renew=now, last_progress_check=now), resources(), now)
    assert bool(stopped) is (expected != 'running')
    assert bool(settled) is (expected in {'cancel', 'timeout'} or cause == 'lease')
    accepted = await runtime.fail_run(rid, token, 'MODEL_CALL_FAILED')
    assert accepted is (expected in {'running', 'cancel', 'timeout'})
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status,error_code FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
        if expected == 'cancel': assert row == {'status': 'cancelled', 'error_code': None}
        if expected == 'timeout': assert row == {'status': 'failed', 'error_code': 'RUN_TIMEOUT'}


@pytest.mark.parametrize('kind', ['model', 'tool'])
async def test_already_settled_acknowledgement_is_not_new_authorization(kind):
    rid, token, call = await _run(kind)
    settle = runtime.settle_model if kind == 'model' else runtime.settle_tool
    assert isinstance(await settle(rid, token, call, 'succeeded'), Applied)
    async with transaction() as conn:
        await _revoke(conn, rid, 'takeover')
        before = await (await conn.execute(f'SELECT * FROM agent.{kind}_calls WHERE id=%s', (call,))).fetchone()
    assert isinstance(await settle(rid, token, call, 'succeeded'), AlreadyApplied)
    assert isinstance(await settle(rid, token, call, 'failed', error_code='OTHER'), Rejected)
    with pytest.raises(PermissionError): await store.check(TaskRef(rid, TaskKind.AGENT), token)
    async with transaction(readonly=True) as conn:
        assert await (await conn.execute(f'SELECT * FROM agent.{kind}_calls WHERE id=%s', (call,))).fetchone() == before
