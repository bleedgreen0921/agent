from tests.native_async import native, native_runnable
'Failure boundaries and Worker status without real model or document data.'
import os
import threading
import time
from uuid import uuid4
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from agent_service.app import app as agent_app
from agent_service import memory
from agent_service import worker as agent_worker
from db.config_check import validate_role
from db.connection import bounded_conninfo, connect
from execution import store
from execution.contracts import TaskRef, TaskKind
from db.worker_status import beat, remove, snapshot
from rag_service import worker
from rag_service.app import app as rag_app
from rag_service.chunking import ChunkDraft
HAS_DATABASE = all((os.environ.get(name) for name in ('AGENT_DATABASE_URL', 'RAG_DATABASE_URL')))

def _delete_document_probe(document_id):
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute('UPDATE rag.documents SET active_version_id=NULL WHERE id=%s', (document_id,))
        conn.execute('DELETE FROM rag.processing_jobs WHERE version_id IN (SELECT id FROM rag.document_versions WHERE document_id=%s)', (document_id,))
        conn.execute('DELETE FROM rag.chunks WHERE document_id=%s', (document_id,))
        conn.execute('DELETE FROM rag.document_versions WHERE document_id=%s', (document_id,))
        conn.execute('DELETE FROM rag.documents WHERE id=%s', (document_id,))

def test_database_profiles_preserve_dsn_options_and_bound_waits(monkeypatch):
    monkeypatch.setenv('DB_CONNECT_TIMEOUT_SECONDS', '10')
    monkeypatch.setenv('DB_CONTROL_STATEMENT_TIMEOUT_SECONDS', '15')
    dsn, timeout = bounded_conninfo('postgresql://user:secret@localhost/db?options=-c%20search_path%3Dagent', 'control')
    settings = conninfo_to_dict(dsn)
    assert timeout == 10
    assert 'search_path=agent' in settings['options']
    assert 'statement_timeout=15000' in settings['options']
    assert 'lock_timeout=5000' in settings['options']
    monkeypatch.setenv('DB_CONTROL_STATEMENT_TIMEOUT_SECONDS', '0')
    with pytest.raises(ValueError, match='DB_CONTROL_STATEMENT_TIMEOUT_SECONDS'):
        bounded_conninfo(dsn, 'control')

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
def test_database_statement_wait_is_bounded(monkeypatch):
    monkeypatch.setenv('DB_CONTROL_STATEMENT_TIMEOUT_SECONDS', '1')
    with pytest.raises(psycopg.errors.QueryCanceled):
        with connect('AGENT_DATABASE_URL', profile='control') as conn:
            conn.execute('SELECT pg_sleep(2)')

def test_role_configuration_fails_early_and_keeps_optional_models_optional(monkeypatch):
    monkeypatch.setenv('AGENT_DATABASE_URL', 'postgresql://user:secret@localhost/db')
    monkeypatch.setenv('AGENT_MODEL_URL', 'http://model.test')
    monkeypatch.setenv('AGENT_MODEL', 'mock')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 's' * 43)
    validate_role('agent-worker')
    monkeypatch.setenv('AGENT_HEARTBEAT_SECONDS', '60')
    with pytest.raises(ValueError, match='AGENT_HEARTBEAT_SECONDS'):
        validate_role('agent-worker')
    monkeypatch.setenv('AGENT_HEARTBEAT_SECONDS', '30')
    monkeypatch.setenv('AGENT_MODEL_URL', 'ftp://model.test')
    with pytest.raises(ValueError, match='AGENT_MODEL_URL'):
        validate_role('agent-worker')

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_worker_heartbeats_and_admin_status_are_read_only(monkeypatch):
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 's' * 43)
    ids = {'agent': uuid4(), 'rag': uuid4()}
    try:
        await native(beat, 'agent', ids['agent'], memory_consumer_alive=True)
        await native(beat, 'rag', ids['rag'])
        for service, app in (('agent', agent_app), ('rag', rag_app)):
            state = snapshot(service)
            assert state['online_count'] >= 1
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
                assert client.get('/v1/admin/workers').status_code == 404
                response = client.get('/v1/workers')
                assert response.status_code == 200, response.text
                assert response.json()['service'] == service
                assert any((row['instance_id'] == str(ids[service]) for row in response.json()['instances']))
        with connect('AGENT_DATABASE_URL') as conn:
            conn.execute("UPDATE agent.worker_instances SET heartbeat_at=now()-interval '2 minutes' WHERE instance_id=%s", (ids['agent'],))
        assert any((not row['online'] for row in snapshot('agent')['instances'] if row['instance_id'] == ids['agent']))
    finally:
        for service, instance_id in ids.items():
            await native(remove, service, instance_id)

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_document_timeout_is_terminal_even_for_retryable_failure():
    document_id, version_id, job_id, token = (uuid4() for _ in range(4))
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'timeout probe')", (document_id,))
        conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)\n            VALUES (%s,%s,1,%s,'/unused','text/plain','processing')", (version_id, document_id, bytes(32)))
        await native(store.create, conn, TaskRef(job_id, TaskKind.DOCUMENT))
        conn.execute("UPDATE execution.tasks SET status='running',attempts=1,lease_token=%s,started_at=now(),execution_deadline_at=now()-interval '1 second',leased_until=now()+interval '1 minute' WHERE id=%s", (token, job_id))
        conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (job_id, version_id))
    await native(worker.fail, {'id': job_id, 'version_id': version_id, 'token': token, 'attempts': 1}, 'DATABASE_UNAVAILABLE', True)
    with connect('RAG_DATABASE_URL') as conn:
        job = conn.execute('SELECT status,error_code FROM rag.processing_job_state WHERE id=%s', (job_id,)).fetchone()
        version = conn.execute('SELECT status,error_code FROM rag.processing_job_state WHERE version_id=%s', (version_id,)).fetchone()
    assert job == version == {'status': 'failed', 'error_code': 'PROCESSING_TIMEOUT'}
    _delete_document_probe(document_id)

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_document_deadline_survives_retry_and_expired_job_is_settled(monkeypatch):
    document_id, version_id, job_id = (uuid4() for _ in range(3))
    monkeypatch.setenv('RAG_DOCUMENT_TIMEOUT_SECONDS', '14400')
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'deadline probe')", (document_id,))
        conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)\n            VALUES (%s,%s,1,%s,'/unused','text/plain','pending')", (version_id, document_id, bytes(32)))
        await native(store.create, conn, TaskRef(job_id, TaskKind.DOCUMENT))
        conn.execute("UPDATE execution.tasks SET available_at='2000-01-01' WHERE id=%s", (job_id,))
        conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (job_id, version_id))
    first = await native(worker.claim)
    assert first['id'] == job_id
    assert 14300 < first['remaining_seconds'] <= 14400
    await native(worker.fail, first, 'DATABASE_UNAVAILABLE', True)
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET available_at='2000-01-01' WHERE id=%s", (job_id,))
    second = await native(worker.claim)
    assert second['id'] == job_id
    assert second['deadline_at'] == first['deadline_at']
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET execution_deadline_at=now()-interval '1 second' WHERE id=%s", (job_id,))
    await native(worker.claim)
    with connect('RAG_DATABASE_URL') as conn:
        job = conn.execute('SELECT status,error_code FROM rag.processing_job_state WHERE id=%s', (job_id,)).fetchone()
        version = conn.execute('SELECT status,error_code FROM rag.processing_job_state WHERE version_id=%s', (version_id,)).fetchone()
    assert job == version == {'status': 'failed', 'error_code': 'PROCESSING_TIMEOUT'}
    _delete_document_probe(document_id)

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
@pytest.mark.parametrize('expire_before_commit', [False, True])
async def test_document_publish_allows_heartbeat_before_final_lease_check(monkeypatch, expire_before_commit):
    from rag_service.storage import jobs
    from execution.persistence import resources
    from tests.native_async import runtimes
    import asyncio
    document_id, version_id, job_id, token = (uuid4() for _ in range(4))
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'publish probe')", (document_id,))
        conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,1,%s,'/unused','text/plain','processing')", (version_id, document_id, bytes(32)))
        await native(store.create, conn, TaskRef(job_id, TaskKind.DOCUMENT))
        conn.execute("UPDATE execution.tasks SET status='running',attempts=1,lease_token=%s,started_at=clock_timestamp(),execution_deadline_at=clock_timestamp()+interval '4 hours',leased_until=clock_timestamp()+interval '1 minute' WHERE id=%s", (token, job_id))
        conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (job_id, version_id))
        revision = conn.execute("SELECT id,model,dimensions FROM rag.index_revisions WHERE state='active'").fetchone()
    job = {'id': job_id, 'version_id': version_id, 'token': token}
    entered, released = (asyncio.Event(), asyncio.Event())
    original = jobs.transaction
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        async with original(*args, **kwargs) as conn:

            class Proxy:

                async def execute(self, sql, *params):
                    result = await conn.execute(sql, *params)
                    if 'INSERT INTO rag.chunks' in sql:
                        entered.set()
                        await released.wait()
                    return result
            yield Proxy()
    monkeypatch.setattr(jobs, 'transaction', transaction)
    draft = {'content': 'publish content', 'heading_path': [], 'locator': {'kind': 'txt', 'line_start': 1, 'line_end': 1}, 'table_html': None, 'search_text': 'publish content'}
    publisher = asyncio.create_task(native(jobs.publish, job, {'document_id': document_id}, [draft], [], [(revision, [[0.1] * revision['dimensions']])]))
    await asyncio.wait_for(entered.wait(), 3)
    assert await native(store.renew, TaskRef(job_id, TaskKind.DOCUMENT), token, 60)
    if expire_before_commit:
        with connect('RAG_DATABASE_URL') as conn:
            conn.execute("UPDATE execution.tasks SET execution_deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s", (job_id,))
    released.set()
    if expire_before_commit:
        with pytest.raises(PermissionError):
            await publisher
    else:
        await publisher
    with connect('RAG_DATABASE_URL') as conn:
        count = conn.execute('SELECT count(*) AS n FROM rag.chunks WHERE version_id=%s', (version_id,)).fetchone()['n']
    assert count == (0 if expire_before_commit else 1)
