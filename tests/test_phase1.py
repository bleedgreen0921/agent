from tests.native_async import native, native_runnable
"""Personal API perimeter, service correlation and fresh-database safety."""
import os
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit
import psycopg
import pytest
from fastapi.testclient import TestClient
from contracts.v1 import RunCreate
from pydantic import ValidationError
from agent_service.app import app as agent
from rag_service.app import app as rag
from db import migrate, readiness, doctor
from db.connection import connect
from execution.contracts import TaskRef, TaskKind
from execution import store

def local(app):
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))

@pytest.mark.parametrize('app', [agent, rag])
@pytest.mark.parametrize('peer,host,origin,allowed', [('127.0.0.1', '127.0.0.1', None, True), ('::1', '[::1]:8001', 'http://localhost:9000', True), ('192.168.1.2', 'localhost', None, False), ('127.0.0.1', 'evil.test', None, False), ('127.0.0.1', '127.0.0.1', 'https://evil.test', False), ('127.0.0.1', 'localhost', 'null', False), ('127.0.0.1', 'localhost', 'http://localhost@evil.test', False), ('127.0.0.1', 'localhost', 'http://localhost/evil', False)])
def test_local_access(app, peer, host, origin, allowed):
    headers = {'Host': host, 'X-Forwarded-For': '127.0.0.1', 'X-Forwarded-Host': 'localhost'}
    if origin:
        headers['Origin'] = origin
    with TestClient(app, base_url='http://localhost', client=(peer, 50000)) as client:
        response = client.get('/health', headers=headers)
        assert response.status_code == (200 if allowed else 403)
        if not allowed:
            from contracts.v1 import ErrorResponse
            payload = ErrorResponse.model_validate(response.json())
            assert response.headers['X-Request-Id'] == payload.request_id
            assert payload.error.code == 'FORBIDDEN'

def test_no_identity_or_management_api():
    for app in (agent, rag):
        schema = app.openapi()
        encoded = str(schema)
        assert not any((key in encoded for key in ('team_id', 'user_id', 'key_id', 'X-Team-Id', 'visibility')))
        assert all(('/admin/' not in p and '/team/' not in p for p in schema['paths']))
        with local(app) as client:
            for path in ('/v1/admin/teams', '/v1/team/users', '/v1/admin/workers', '/v1/admin/runs'):
                assert client.get(path).status_code == 404
    assert list(agent.openapi()['paths']).index('/v1/runs/summary') < list(agent.openapi()['paths']).index('/v1/runs/{run_id}')
    with pytest.raises(ValidationError):
        RunCreate(task='x', team_id=str(uuid4()))

@pytest.mark.parametrize('token', [None, 'wrong'])
def test_evidence_keeps_independent_service_auth(monkeypatch, token):
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 's' * 43)
    headers = {'X-Run-Id': 'run', 'X-Tool-Call-Id': 'call'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with local(rag) as client:
        assert client.post('/v1/evidence/search', headers=headers, json={'query': 'x'}).status_code == 401

def test_evidence_requires_correlation(monkeypatch):
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 's' * 43)
    with local(rag) as client:
        for missing in ('X-Run-Id', 'X-Tool-Call-Id'):
            headers = {'Authorization': 'Bearer ' + 's' * 43, 'X-Run-Id': 'run', 'X-Tool-Call-Id': 'call'}
            del headers[missing]
            assert client.post('/v1/evidence/search', headers=headers, json={'query': 'x'}).status_code == 422

@pytest.mark.skipif(not os.environ.get('MIGRATION_DATABASE_URL'), reason='isolated PostgreSQL not configured')
async def test_personal_schema_roles_and_doctor():
    assert readiness.agent_ready() and readiness.rag_ready()
    report = doctor.run_checks()
    assert report['status'] == 'ok', report
    with connect('MIGRATION_DATABASE_URL') as conn:
        assert not conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='identity' OR (table_schema IN ('agent','rag') AND table_name IN (SELECT tablename FROM pg_tables WHERE schemaname IN ('agent','rag')) AND column_name IN ('team_id','user_id','lease_token','leased_until','attempts','visibility'))").fetchone()
        for table in ('agent.agent_runs', 'agent.memory_jobs', 'rag.processing_jobs'):
            assert not conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=split_part(%s,'.',1) AND table_name=split_part(%s,'.',2) AND column_name='status'", (table, table)).fetchone()
    for env, denied in (('AGENT_DATABASE_URL', TaskKind.DOCUMENT), ('RAG_DATABASE_URL', TaskKind.AGENT)):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect(env) as conn:
                await native(store.create, conn, TaskRef(uuid4(), denied))
    migrate.upgrade('execution')
    migrate.upgrade('rag')
    migrate.upgrade('agent')

@pytest.mark.skipif(not os.environ.get('MIGRATION_DATABASE_URL'), reason='isolated PostgreSQL not configured')
@pytest.mark.parametrize('legacy', ['identity', 'revision', 'fields', 'unrecognized'])
def test_old_database_refused_without_mutation(monkeypatch, legacy):
    from psycopg import sql
    original = os.environ['MIGRATION_DATABASE_URL']
    name = 'legacy_probe_' + uuid4().hex
    with psycopg.connect(original, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    parsed = urlsplit(original)
    dsn = urlunsplit(parsed._replace(path='/' + name))
    try:
        with psycopg.connect(dsn) as conn:
            if legacy == 'identity':
                conn.execute('CREATE SCHEMA identity')
                conn.execute('CREATE TABLE identity.teams(id uuid)')
            elif legacy == 'revision':
                conn.execute('CREATE SCHEMA agent')
                conn.execute('CREATE TABLE agent.alembic_version(version_num text)')
                conn.execute("INSERT INTO agent.alembic_version VALUES ('agent_0009')")
            elif legacy == 'fields':
                conn.execute('CREATE SCHEMA rag')
                conn.execute('CREATE TABLE rag.documents(team_id uuid)')
            else:
                conn.execute('CREATE TABLE public.keep_me(value text)')
                conn.execute("INSERT INTO public.keep_me VALUES ('preserved')")
            before = conn.execute("SELECT table_schema,table_name,column_name FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3").fetchall()
        monkeypatch.setenv('MIGRATION_DATABASE_URL', dsn)
        with pytest.raises(RuntimeError):
            migrate.upgrade('execution')
        with psycopg.connect(dsn) as conn:
            after = conn.execute("SELECT table_schema,table_name,column_name FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,3").fetchall()
            assert before == after
            if legacy == 'unrecognized':
                assert conn.execute('SELECT value FROM keep_me').fetchone()[0] == 'preserved'
    finally:
        with psycopg.connect(original, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

@pytest.mark.skipif(not os.environ.get('AGENT_DATABASE_URL'), reason='isolated PostgreSQL not configured')
async def test_agent_rag_adapter_keeps_service_token_and_correlation_without_team():
    import httpx
    from tests.helpers import insert_run
    from agent_service.rag_client import RagClient, RagError
    from contracts.v1 import EvidenceSearchRequest, ErrorCode
    from tests.test_execution_contract import evidence
    run_id = insert_run()
    received = []

    def handler(request):
        received.append(request)
        assert request.headers['Authorization'] == 'Bearer ' + 's' * 43
        assert request.headers['X-Run-Id'] == str(run_id)
        assert request.headers['X-Tool-Call-Id'] == 'call_probe'
        assert 'X-Team-Id' not in request.headers
        item = evidence()
        return httpx.Response(200, json={'request_id': 'request', 'retrieval_id': 'retrieval', 'status': 'ok', 'evidences': [item], 'degradations': []} if request.method == 'POST' else item)
    from execution.persistence import resources
    resources().http.transports['rag']=httpx.MockTransport(handler)
    if True:
        client = RagClient('http://localhost:8001', 's' * 43)
        assert (await native(client.search, run_id, 'call_probe', EvidenceSearchRequest(query='x'))).evidences[0].evidence_id == 'ev_shared'
        assert (await native(client.read, run_id, 'call_probe', 'ev_shared')).content == 'same'
    assert len(received) == 2
