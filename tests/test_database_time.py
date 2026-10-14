"""PostgreSQL time semantics under real lock waits and expired transactions."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
import os
from uuid import uuid4

import pytest

from agent_service import runtime as agent_runtime
from agent_service.storage import runtime as agent_store, operations
from db import worker_status, async_queries, doctor
from execution import store
from execution.contracts import TaskKind, TaskRef
from execution.infra.types import OperationDeadline
from execution.persistence import transaction, using
from rag_service.storage import documents
from tests.helpers import insert_run
from tests.native_async import runtimes

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


@pytest.mark.parametrize('operation', ['agent_beat', 'rag_beat', 'delete', 'model', 'tool', 'manifest'])
async def test_event_timestamps_are_after_actual_lock_release(monkeypatch, operation):
    service = 'rag' if operation in {'rag_beat', 'delete'} else 'agent'
    target = worker_status if operation.endswith('_beat') else documents if operation == 'delete' else agent_store
    resource = runtimes.get()[service]
    with using(resource):
        entity = uuid4()
        if operation.endswith('_beat'):
            await worker_status.beat(service, entity)
            table = worker_status.TABLES[service][1]
            lock_sql = f'SELECT instance_id FROM {table} WHERE instance_id=%s FOR UPDATE'
        elif operation == 'delete':
            async with transaction() as conn:
                await conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'clock probe')", (entity,))
            lock_sql = 'SELECT id FROM rag.documents WHERE id=%s FOR UPDATE'
        else:
            entity = insert_run()
            job = await agent_store.claim_run() if operation != 'manifest' else None
            lock_sql = 'SELECT pg_advisory_xact_lock(%s)' if operation == 'manifest' else 'SELECT id FROM execution.tasks WHERE id=%s FOR UPDATE'

        attempted = asyncio.Event()
        backend = []
        original = target.connect
        @asynccontextmanager
        async def traced(*args, **kwargs):
            async with original(*args, **kwargs) as conn:
                class Proxy:
                    async def execute(self, query, *params):
                        if ('FOR UPDATE' in query or 'pg_advisory_xact_lock' in query or
                            operation.endswith('_beat') and 'ON CONFLICT' in query):
                            backend[:] = [conn.info.backend_pid]
                            attempted.set()
                        return await conn.execute(query, *params)
                yield Proxy()
        monkeypatch.setattr(target, 'connect', traced)

        async def candidate():
            if operation.endswith('_beat'):
                return await worker_status.beat(service, entity)
            if operation == 'delete':
                return await documents.soft_delete(entity)
            if operation == 'model':
                return await agent_store.reserve_model(entity, job['lease_token'], 'react', 'mock')
            if operation == 'tool':
                return await agent_store.reserve_tool(entity, job['lease_token'], 'probe', {})
            return await agent_store.claim_run()

        pending = None
        try:
            async with transaction() as holder:
                await holder.execute(lock_sql, (824731091 if operation == 'manifest' else entity,))
                pending = asyncio.create_task(candidate())
                async with asyncio.timeout(5):
                    await attempted.wait()
                    async with transaction(readonly=True) as monitor:
                        while True:
                            row = await (await monitor.execute('SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked',
                                (holder.info.backend_pid, backend[0]))).fetchone()
                            if row['blocked']:
                                break
                            assert not pending.done(), 'Operation did not wait on the expected lock'
                            await asyncio.sleep(0)
                released_at = (await (await holder.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
            async with asyncio.timeout(5):
                result = await pending
            async with transaction(readonly=True) as conn:
                if operation.endswith('_beat'):
                    event = await (await conn.execute(f'SELECT heartbeat_at AS at FROM {table} WHERE instance_id=%s', (entity,))).fetchone()
                elif operation == 'delete':
                    event = {'at': result['deleted_at']}
                    # Deletion is still idempotent and retains the first timestamp.
                    assert (await documents.soft_delete(entity))['deleted_at'] == event['at']
                elif operation in {'model', 'tool'}:
                    event = await (await conn.execute(f'SELECT started_at AS at FROM agent.{operation}_calls WHERE id=%s', (result,))).fetchone()
                else:
                    event = await (await conn.execute('SELECT captured_at AS at FROM agent.run_manifests WHERE run_id=%s', (entity,))).fetchone()
                    assert result['run_id'] == entity
                assert event['at'] >= released_at
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('kind', list(TaskKind))
@pytest.mark.parametrize('expiry', ['queue', 'execution'])
async def test_claim_rechecks_time_after_candidate_selection(kind, expiry):
    with using(runtimes.get()['rag' if kind == TaskKind.DOCUMENT else 'agent']):
        task = TaskRef(uuid4(), kind)
        async with transaction() as conn:
            await store.create(conn, task, queue_seconds=60)
            initial = await store.claim(conn, kind, lease_seconds=60, sweep_expired=False) if expiry == 'execution' else None
            column = 'execution_deadline_at' if initial else 'queue_deadline_at'
            if initial:
                await conn.execute("UPDATE execution.tasks SET status='queued',lease_token=NULL,leased_until=NULL WHERE id=%s", (task.id,))
            await conn.execute(f"UPDATE execution.tasks SET {column}=clock_timestamp()+interval '0.2 seconds' WHERE id=%s", (task.id,))
            selected = []
            class DelayedCursor:
                def __init__(self, cursor): self.cursor = cursor
                async def fetchone(self):
                    row = await self.cursor.fetchone()
                    assert row is not None
                    selected.append(row)
                    await conn.execute('SELECT pg_sleep_until(%s)', (row[column],))
                    clock = await (await conn.execute('SELECT transaction_timestamp() AS tx,clock_timestamp() AS at')).fetchone()
                    assert clock['tx'] < row[column] <= clock['at']
                    return row
            class Proxy:
                async def execute(self, query, *params):
                    cursor = await conn.execute(query, *params)
                    if 'FOR UPDATE OF t SKIP LOCKED LIMIT 1' in query:
                        return DelayedCursor(cursor)
                    return cursor
            assert await store.claim(Proxy(), kind, lease_seconds=60, sweep_expired=False) is None
            state = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s', (task.id,))).fetchone()
            assert len(selected) == 1
            assert state['status'] == 'failed' and state['lease_token'] is None
            assert state['error_code'] == ('QUEUE_TIMEOUT' if expiry == 'queue' else store.TIMEOUT_CODE[kind])
            assert state['attempts'] == (1 if initial else 0)
            assert state['started_at'] == (initial['started_at'] if initial else None)


async def test_recovery_expiring_during_validation_is_failed_instead_of_requeued(monkeypatch):
    entity = insert_run()
    await agent_store.claim_run()
    async with transaction() as conn:
        await conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()-interval '1 second',execution_deadline_at=clock_timestamp()+interval '0.2 seconds' WHERE id=%s", (entity,))
        async def slow_validation(current, run_id):
            assert run_id == entity
            expires = (await (await current.execute('SELECT execution_deadline_at FROM execution.tasks WHERE id=%s', (entity,))).fetchone())['execution_deadline_at']
            await current.execute('SELECT pg_sleep_until(%s)', (expires,))
            return True
        monkeypatch.setattr(agent_runtime, 'safe_to_resume', slow_validation)
        await store.sweep(conn, TaskKind.AGENT)
        row = await (await conn.execute('SELECT status,error_code,attempts FROM execution.tasks WHERE id=%s', (entity,))).fetchone()
        assert row == {'status': 'failed', 'error_code': 'RUN_TIMEOUT', 'attempts': 1}


@pytest.mark.parametrize('kind', list(TaskKind))
async def test_creation_claim_and_renewal_use_consistent_fresh_time_bases(kind):
    with using(runtimes.get()['rag' if kind == TaskKind.DOCUMENT else 'agent']):
        task = TaskRef(uuid4(), kind)
        async with transaction() as conn:
            created = await store.create(conn, task, queue_seconds=60)
            assert created['created_at'] == created['updated_at'] == created['available_at']
            assert created['queue_deadline_at'] - created['created_at'] == timedelta(seconds=60)
            # Transaction time must not be reused as the first execution start.
            after = (await (await conn.execute("SELECT clock_timestamp()+interval '0.05 seconds' AS at")).fetchone())['at']
            await conn.execute('SELECT pg_sleep_until(%s)', (after,))
            job = await store.claim(conn, kind, lease_seconds=60)
            assert job['started_at'] >= after
            assert job['started_at'] == job['heartbeat_at'] == job['updated_at']
            assert job['execution_deadline_at'] - job['started_at'] == timedelta(seconds=store.DEFAULT_SECONDS[kind])
            assert job['leased_until'] - job['started_at'] == timedelta(seconds=60)
        assert await store.renew(task, job['lease_token'], 30)
        async with transaction(readonly=True) as conn:
            renewed = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s', (task.id,))).fetchone()
        assert renewed['heartbeat_at'] == renewed['updated_at']
        assert renewed['leased_until'] - renewed['heartbeat_at'] == timedelta(seconds=30)
        assert renewed['execution_deadline_at'] == job['execution_deadline_at']
        assert renewed['started_at'] == job['started_at']


@pytest.mark.parametrize('service', ['agent', 'rag'])
async def test_worker_report_samples_actual_time_and_reuses_one_cutoff(monkeypatch, service):
    resource = runtimes.get()[service]
    with using(resource):
        entity = uuid4()
        await worker_status.beat(service, entity)
        original = resource.db.transaction
        samples, used = [], []
        @asynccontextmanager
        async def aged(*args, **kwargs):
            async with original(*args, **kwargs) as conn:
                row = await (await conn.execute("SELECT transaction_timestamp() AS tx,clock_timestamp()+interval '0.15 seconds' AS cutoff")).fetchone()
                samples.append(row)
                async with original('control', OperationDeadline.after(3)) as writer:
                    await writer.execute(f'UPDATE {worker_status.TABLES[service][1]} SET heartbeat_at=%s WHERE instance_id=%s',
                        (row['cutoff'] - timedelta(seconds=90), entity))
                await conn.execute('SELECT pg_sleep_until(%s)', (row['cutoff'],))
                class Proxy:
                    async def execute(self, query, *params):
                        if 'WITH clock AS' in query:
                            used.append(params[0][0])
                        return await conn.execute(query, *params)
                yield Proxy()
        monkeypatch.setattr(resource.db, 'transaction', aged)
        result = await async_queries.worker_snapshot(resource, service)
        assert samples[0]['tx'] < samples[0]['cutoff'] <= result['observed_at']
        assert result['online_count'] == 0 and result['instances'][0]['online'] is False
        assert used == [result['observed_at']] * 3


@pytest.mark.parametrize('check', ['run', 'memory', 'document'])
def test_doctor_expiry_uses_actual_time_after_transaction_started(monkeypatch, check):
    from db.connection import connect
    kind = {'run': TaskKind.AGENT, 'memory': TaskKind.MEMORY, 'document': TaskKind.DOCUMENT}[check]
    task = uuid4()
    env = store.ENV[kind]
    with connect(env) as conn:
        conn.execute("""INSERT INTO execution.tasks(id,kind,max_attempts,status,attempts,started_at,execution_deadline_at,leased_until,lease_token)
            VALUES (%s,%s,3,'running',1,clock_timestamp(),clock_timestamp()+interval '1 hour',clock_timestamp()+interval '1 minute',%s)""",
            (task, kind, uuid4()))
        if check == 'run':
            conn.execute("INSERT INTO agent.agent_runs(id,task,mode,request_digest,graph_version) VALUES (%s,'clock probe','react',%s,'v2')", (task, bytes(32)))
        elif check == 'document':
            doc, version = uuid4(), uuid4()
            conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'clock probe')", (doc,))
            conn.execute("""INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)
                VALUES (%s,%s,1,%s,'/unused','text/plain','processing')""", (version, doc, bytes(32)))
            conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (task, version))
        else:
            conversation, turn, run = uuid4(), uuid4(), insert_run(conn=conn)
            conn.execute('INSERT INTO agent.conversations(id) VALUES (%s)', (conversation,))
            conn.execute("INSERT INTO agent.conversation_turns(id,conversation_id,ordinal,user_text,run_id,idempotency_key,request_digest) VALUES (%s,%s,1,'probe',%s,'clock-probe',%s)", (turn, conversation, run, bytes(32)))
            conn.execute("INSERT INTO agent.memory_jobs(id,kind,turn_id) VALUES (%s,'extract',%s)", (task, turn))
    original = doctor.readonly_connection
    @contextmanager
    def aged(env_name):
        with original(env_name) as reader:
            times = reader.execute("SELECT transaction_timestamp() AS tx,clock_timestamp()+interval '0.15 seconds' AS cutoff").fetchone()
            with connect(env_name) as writer:
                writer.execute('UPDATE execution.tasks SET leased_until=%s WHERE id=%s', (times['cutoff'], task))
            reader.execute('SELECT pg_sleep_until(%s)', (times['cutoff'],))
            assert reader.execute('SELECT clock_timestamp()>=%s AND transaction_timestamp()<%s AS crossed', (times['cutoff'], times['cutoff'])).fetchone()['crossed']
            yield reader
    monkeypatch.setattr(doctor, 'readonly_connection', aged)
    report = {'run': doctor.check_run_leases, 'memory': doctor.check_memory_jobs, 'document': doctor.check_processing_leases}[check]()
    assert report['status'] == 'warn'
    if check == 'memory':
        assert report['details']['expired_samples'] == [str(task)]
    else:
        assert report['details']['count'] == 1 and report['details']['samples'] == [str(task)]


async def test_queue_summary_reuses_cutoff_when_deadline_passes_between_queries(monkeypatch):
    entity = insert_run()
    original = operations.connect
    times = []
    @asynccontextmanager
    async def delayed(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            async with original('AGENT_DATABASE_URL') as writer:
                row = await (await writer.execute("UPDATE execution.tasks SET queue_deadline_at=clock_timestamp()+interval '0.2 seconds' WHERE id=%s RETURNING queue_deadline_at,created_at", (entity,))).fetchone()
                times.append(row)
            class Proxy:
                async def execute(self, query, *params):
                    cursor = await conn.execute(query, *params)
                    if 'queued_within_deadline' in query:
                        assert params[0][0] < row['queue_deadline_at']
                        await conn.execute('SELECT pg_sleep_until(%s)', (row['queue_deadline_at'],))
                    return cursor
            yield Proxy()
    monkeypatch.setattr(operations, 'connect', delayed)
    report = await operations.run_queue_summary()
    assert report['queued_within_deadline'] == 1 and report['queued_past_deadline'] == 0
    assert report['oldest_claimable_created_at'] == times[0]['created_at']


@pytest.mark.parametrize('kind', list(TaskKind))
async def test_retry_transition_rechecks_expiry_after_failure_classification(kind):
    with using(runtimes.get()['rag' if kind == TaskKind.DOCUMENT else 'agent']):
        task = TaskRef(uuid4(), kind)
        async with transaction() as conn:
            await store.create(conn, task)
            job = await store.claim(conn, kind, lease_seconds=60)
            expires = (await (await conn.execute("UPDATE execution.tasks SET execution_deadline_at=clock_timestamp()+interval '0.2 seconds' WHERE id=%s RETURNING execution_deadline_at", (task.id,))).fetchone())['execution_deadline_at']
            class DelayedClock:
                def __init__(self, cursor): self.cursor = cursor
                async def fetchone(self):
                    observed = await self.cursor.fetchone()
                    assert observed['now'] < expires
                    await conn.execute('SELECT pg_sleep_until(%s)', (expires,))
                    return observed
            class Proxy:
                async def execute(self, query, *params):
                    cursor = await conn.execute(query, *params)
                    return DelayedClock(cursor) if query == 'SELECT clock_timestamp() AS now' else cursor
            result = await store.settle_failure(Proxy(), task, job['lease_token'], 'TEMPORARY_FAILURE', retryable=True)
            assert result.accepted and result.status == 'failed' and result.error_code == store.TIMEOUT_CODE[kind]
            state = await (await conn.execute('SELECT status,attempts,lease_token,execution_deadline_at FROM execution.tasks WHERE id=%s', (task.id,))).fetchone()
            assert state == {'status': 'failed', 'attempts': 1, 'lease_token': None, 'execution_deadline_at': expires}
