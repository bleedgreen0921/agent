"""Real PostgreSQL boundaries; executed by the isolated full integration suite."""
import asyncio
from dataclasses import replace
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from db.connection import connect
from db.async_queries import ready
from execution.infra.runtime import RuntimeResources
from execution.infra.types import OperationDeadline as Deadline, ResourceConfig, CapacityTimeout
from execution.contracts import TaskKind, TaskRef
from execution import store

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


@pytest.mark.parametrize('profile,statement,lock', [('control',15000,5000), ('normal',120000,30000), ('bulk',3600000,60000)])
def test_profile_session_settings_transaction_local_reset_and_same_connection(profile,statement,lock):
    async def check():
        config = replace(ResourceConfig(), db_control=1, db_normal=1, db_bulk=1)
        async with RuntimeResources('agent-worker', config=config) as r:
            pool = r.db.pools[profile]
            async with r.db.transaction(profile, Deadline.after(1), readonly=True) as conn:
                pid = (await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid']
                row = await (await conn.execute("SELECT current_setting('transaction_read_only') AS ro, current_setting('statement_timeout') AS st")).fetchone()
                assert row['ro'] == 'on' and row['st'] != f'{statement}ms'
                assert (await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid'] == pid
            # Borrow directly to inspect fixed defaults outside a transaction.
            async with pool.connection() as conn:
                assert (await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid'] == pid
                row = await (await conn.execute("SELECT current_setting('transaction_read_only') AS ro, extract(epoch FROM current_setting('statement_timeout')::interval)*1000 AS st, extract(epoch FROM current_setting('lock_timeout')::interval)*1000 AS lt")).fetchone()
                assert row['ro'] == 'off' and row['st'] == statement and row['lt'] == lock
            async with r.db.transaction(profile, Deadline.after(1)) as conn:
                assert conn.info.transaction_status == TransactionStatus.INTRANS
                assert (await (await conn.execute("SHOW transaction_read_only")).fetchone())['transaction_read_only'] == 'off'
    asyncio.run(check())


def test_pool_isolation_connection_admission_timeout_and_quiesce_wakes():
    async def check():
        config = replace(ResourceConfig(), db_control=1, db_normal=1, db_bulk=1, db_acquire=.1)
        async with RuntimeResources('agent-worker', config=config) as r:
            async with r.db.transaction('normal', Deadline.after(3)), r.db.transaction('bulk', Deadline.after(3)):
                async with r.db.transaction('control', Deadline.after(1), readonly=True) as conn:
                    assert await (await conn.execute('SELECT 1')).fetchone()
                with pytest.raises((CapacityTimeout, TimeoutError)):
                    async with r.db.transaction('normal', Deadline.after(.03)):
                        pytest.fail('exhausted pool admitted work')
                async def waiting():
                    async with r.db.transaction('normal', Deadline.after(2)):
                        pass
                task = asyncio.create_task(waiting())
                await asyncio.sleep(.01)
                r.quiesce()
                assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    asyncio.run(check())


def test_repeatable_read_is_transaction_local_and_selected_before_first_snapshot():
    async def check():
        async with RuntimeResources('rag-worker', config=replace(ResourceConfig(), db_normal=1)) as runtime:
            async with runtime.db.transaction('normal', Deadline.after(3), readonly=True, isolation='repeatable read') as conn:
                settings = await (await conn.execute("""SELECT pg_backend_pid() AS pid,
                    current_setting('transaction_isolation') AS isolation,
                    current_setting('transaction_read_only') AS readonly""")).fetchone()
                assert settings['isolation'] == 'repeatable read' and settings['readonly'] == 'on'
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    async with conn.transaction():
                        await conn.execute('UPDATE rag.index_state SET singleton=singleton')
            async with runtime.db.transaction('normal', Deadline.after(3)) as conn:
                following = await (await conn.execute("""SELECT pg_backend_pid() AS pid,
                    current_setting('transaction_isolation') AS isolation,
                    current_setting('transaction_read_only') AS readonly""")).fetchone()
                assert following['pid'] == settings['pid']
                assert following['isolation'] == 'read committed' and following['readonly'] == 'off'
            with pytest.raises(ValueError, match='Unsupported transaction isolation'):
                async with runtime.db.transaction('normal', Deadline.after(3), isolation='invalid; SELECT 1'):
                    pytest.fail('invalid isolation admitted')
    asyncio.run(check())


def test_query_deadline_cancellation_and_sql_error_rollback():
    async def check():
        instance = uuid4()
        async with RuntimeResources('agent-worker', config=replace(ResourceConfig(), db_normal=1)) as r:
            async def operation():
                async with r.db.transaction('normal', Deadline.after(4)) as conn:
                    await conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (instance,))
                    await conn.execute('SELECT pg_sleep(3)')
            task = asyncio.create_task(operation())
            await asyncio.sleep(.1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with pytest.raises((TimeoutError, psycopg.errors.QueryCanceled)):
                async with r.db.transaction('normal', Deadline.after(.04)) as conn:
                    await conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (instance,))
                    await conn.execute('SELECT pg_sleep(1)')
            with pytest.raises(psycopg.errors.DivisionByZero):
                async with r.db.transaction('normal', Deadline.after(1)) as conn:
                    await conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (instance,))
                    await conn.execute('SELECT 1/0')
            async with r.db.transaction('normal', Deadline.after(1), readonly=True) as conn:
                assert await (await conn.execute('SELECT 1 FROM agent.worker_instances WHERE instance_id=%s', (instance,))).fetchone() is None
    asyncio.run(check())


def test_transaction_deadline_includes_python_work_and_lock_wait():
    instance = uuid4()
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (instance,))
    async def check():
        async with RuntimeResources('agent-worker') as r:
            with pytest.raises(TimeoutError):
                async with r.db.transaction('normal', Deadline.after(.03)) as conn:
                    await conn.execute('UPDATE agent.worker_instances SET memory_consumer_alive=true WHERE instance_id=%s', (instance,))
                    await asyncio.sleep(.5)
            # Keep one borrowed async connection locked while another waits.
            async with r.db.transaction('normal', Deadline.after(2)) as lock:
                await lock.execute('UPDATE agent.worker_instances SET memory_consumer_alive=true WHERE instance_id=%s', (instance,))
                with pytest.raises((TimeoutError, psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled)):
                    async with r.db.transaction('control', Deadline.after(.04)) as conn:
                        await conn.execute('UPDATE agent.worker_instances SET memory_consumer_alive=false WHERE instance_id=%s', (instance,))
    asyncio.run(check())


def test_async_role_rls_readonly_enforcement_and_bad_connection_discard():
    refs = [TaskRef(uuid4(), kind) for kind in (TaskKind.AGENT, TaskKind.DOCUMENT)]
    with connect('MIGRATION_DATABASE_URL') as conn:
        for ref in refs:
            conn.execute("INSERT INTO execution.tasks(id,kind,max_attempts,queue_deadline_at) VALUES (%s,%s,3,clock_timestamp()+interval '30 seconds')",(ref.id,ref.kind))
    async def check():
        for role, expected, denied in [('agent-worker',refs[0], 'rag.documents'), ('rag-worker',refs[1], 'agent.agent_runs')]:
            async with RuntimeResources(role, config=replace(ResourceConfig(), db_normal=1)) as r:
                async with r.db.transaction('normal', Deadline.after(1), readonly=True) as conn:
                    rows = await (await conn.execute('SELECT id FROM execution.tasks')).fetchall()
                    assert [row['id'] for row in rows] == [expected.id]
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    async with r.db.transaction('normal', Deadline.after(1), readonly=True) as conn:
                        await conn.execute('UPDATE execution.tasks SET status=status WHERE id=%s', (expected.id,))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    async with r.db.transaction('normal', Deadline.after(1), readonly=True) as conn:
                        await conn.execute(f'SELECT 1 FROM {denied}')
                # Force a disconnect while borrowed; the connection cannot reappear.
                with pytest.raises(psycopg.Error):
                    async with r.db.transaction('normal', Deadline.after(1)) as conn:
                        old = (await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid']
                        await conn.close()
                        await conn.execute('SELECT 1')
                async with r.db.transaction('normal', Deadline.after(2)) as conn:
                    assert (await (await conn.execute('SELECT pg_backend_pid() AS pid')).fetchone())['pid'] != old
                assert await ready(r, role.split('-')[0])
    asyncio.run(check())


def test_apis_async_reads_keep_response_and_unavailable_is_sanitized(monkeypatch):
    from tests.test_phase1 import local
    from agent_service.app import app as agent
    from rag_service.app import app as rag
    from contracts.v1 import WorkerSnapshot
    for app in (agent,rag):
        with local(app) as client:
            assert client.get('/health/ready').status_code == 200
            WorkerSnapshot.model_validate(client.get('/v1/workers').json())
            app.state.resources.loop.call_soon_threadsafe(app.state.resources.quiesce)
            assert client.get('/health/ready').status_code == 503
            response = client.get('/v1/workers')
            assert response.status_code == 503 and response.json()['error']['message'] == 'Worker status unavailable'
            assert client.get('/health').status_code == 200
    monkeypatch.setenv('ASYNC_DB_ACQUIRE_TIMEOUT_SECONDS', '.03')
    monkeypatch.setenv('AGENT_DATABASE_URL', 'postgresql://private:secret@127.0.0.1:9/missing')
    with local(agent) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/health/ready').status_code == 503
        response = client.get('/v1/workers')
        assert response.status_code == 503
        assert 'secret' not in response.text and 'private' not in response.text
