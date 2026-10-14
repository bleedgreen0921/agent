from tests.acceptance_fixtures import fixture_publish_result
from tests.native_async import native, native_runnable
import hashlib
import json
import os
import types
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import psycopg
import pytest
from agent_service import manifest as manifest_module
from agent_service.storage import runtime
from agent_service.runs import timeline
from contracts.errors import ApiError
from contracts.v1 import RunTimeline
from db.connection import connect
from db.doctor import render_text, run_checks
from rag_service import retrieval
from rag_service.models import ModelFailure
HAS_DATABASE = bool(os.environ.get('AGENT_DATABASE_URL'))

def test_manifest_revision_sources_and_secret_exclusion(monkeypatch):
    manifest_module.application_revision.cache_clear()
    monkeypatch.setenv('APP_REVISION', ' release-123 ')
    monkeypatch.setenv('AGENT_MODEL_URL', 'https://model.example.invalid/v1')
    monkeypatch.setenv('AGENT_MODEL_KEY', 'model-secret')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'rag-secret')
    monkeypatch.setenv('AGENT_MODEL', 'test-model')
    value = manifest_module.execution_manifest()
    assert value['application'] == {'revision': 'release-123', 'revision_source': 'environment', 'dirty': False}
    encoded = json.dumps(value)
    assert 'model-secret' not in encoded
    assert 'rag-secret' not in encoded
    assert 'model.example.invalid' not in encoded
    assert value['model']['base_url_sha256'] == hashlib.sha256(b'https://model.example.invalid/v1').hexdigest()
    monkeypatch.delenv('APP_REVISION')
    manifest_module.application_revision.cache_clear()
    outputs = iter([types.SimpleNamespace(stdout='abc123\n'), types.SimpleNamespace(stdout=' M tracked.py\n')])
    monkeypatch.setattr(manifest_module.subprocess, 'run', lambda *args, **kwargs: next(outputs))
    assert manifest_module.application_revision() == {'revision': 'abc123', 'revision_source': 'git', 'dirty': True}
    manifest_module.application_revision.cache_clear()
    monkeypatch.setattr(manifest_module.subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert manifest_module.application_revision() == {'revision': 'unknown', 'revision_source': 'unknown', 'dirty': False}
    manifest_module.application_revision.cache_clear()

def test_doctor_missing_configuration_is_structured(monkeypatch):
    for name in ('MIGRATION_DATABASE_URL', 'RAG_DATABASE_URL', 'AGENT_DATABASE_URL'):
        monkeypatch.delenv(name, raising=False)
    report = run_checks()
    assert report['schema_version'] == 1
    assert report['status'] == 'error'
    required = next((item for item in report['checks'] if item['code'] == 'config.required_env'))
    assert required['status'] == 'error'
    text = render_text(report)
    assert '[ERROR] config.required_env:' in text
    assert 'postgresql://' not in json.dumps(report)
from tests.helpers import quiesce_runs as _quiesce_runs, insert_run

def _queued_run():
    return insert_run(task='private task', created_delta=timedelta(days=-1))

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_manifest_claim_is_atomic_immutable_and_least_privilege(monkeypatch):
    _quiesce_runs()
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '1')
    base_manifest = manifest_module.execution_manifest()
    first_manifest = {**base_manifest, 'marker': 'first'}
    monkeypatch.setattr(runtime, 'execution_manifest', lambda: first_manifest)
    run_id = _queued_run()
    claimed = await native(runtime.claim_run)
    assert claimed and claimed['run_id'] == run_id
    with connect('AGENT_DATABASE_URL') as conn:
        stored = conn.execute('SELECT schema_version,manifest FROM agent.run_manifests WHERE run_id=%s', (run_id,)).fetchone()
        conn.execute("UPDATE execution.tasks SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    assert stored == {'schema_version': 6, 'manifest': first_manifest}
    monkeypatch.setattr(runtime, 'execution_manifest', lambda: {**base_manifest, 'marker': 'second'})
    resumed = await native(runtime.claim_run)
    assert resumed and resumed['run_id'] == run_id and (not resumed['first_claim'])
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT manifest FROM agent.run_manifests WHERE run_id=%s', (run_id,)).fetchone()['manifest'] == first_manifest
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agent.run_manifests SET manifest='{}'::jsonb WHERE run_id=%s", (run_id,))
    _quiesce_runs()
    failed_run_id = _queued_run()
    monkeypatch.setattr(runtime, 'execution_manifest', lambda: {'not_json': {1}})
    with pytest.raises(TypeError):
        await native(runtime.claim_run)
    with connect('AGENT_DATABASE_URL') as conn:
        row = conn.execute('SELECT status,started_at FROM agent.run_state WHERE id=%s', (failed_run_id,)).fetchone()
        captured = conn.execute('SELECT 1 FROM agent.run_manifests WHERE run_id=%s', (failed_run_id,)).fetchone()
    assert row == {'status': 'queued', 'started_at': None}
    assert captured is None
    _quiesce_runs()
    monkeypatch.setattr(runtime, 'execution_manifest', manifest_module.execution_manifest)
    declared_run_id = _queued_run()
    assert (await native(runtime.claim_run))['run_id'] == declared_run_id
    with connect('AGENT_DATABASE_URL') as conn:
        declared = conn.execute('SELECT schema_version,manifest FROM agent.run_manifests WHERE run_id=%s', (declared_run_id,)).fetchone()
    assert declared['schema_version'] == 6
    assert declared['manifest']['tools']['providers'][0]['tools'] == [{'name': 'search_evidence', 'version': '1', 'kind': 'read_only', 'produces_evidence': True, 'intent_spec':None}, {'name': 'read_evidence', 'version': '1', 'kind': 'read_only', 'produces_evidence': True, 'intent_spec':None}]

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_timeline_is_deterministic_linked_and_safe(monkeypatch):
    _quiesce_runs()
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '1')
    monkeypatch.setattr(runtime, 'execution_manifest', lambda: {'safe': True})
    run_id = _queued_run()
    claimed = await native(runtime.claim_run)
    model_id = await native(runtime.reserve_model, run_id, claimed['lease_token'], 'react', 'mock', input_summary={'message_count': 1})
    assert await native(runtime.settle_model, run_id, claimed['lease_token'], model_id, 'succeeded', output_summary={'message_count': 1})
    tool_id = await native(runtime.reserve_tool, run_id, claimed['lease_token'], 'search_evidence', {'arguments_sha256': 'abc'}, triggering_model_call_id=model_id)
    assert await native(runtime.settle_tool, run_id, claimed['lease_token'], tool_id, 'succeeded', service_request_id='rag_req_safe', retrieval_id='ret_safe', evidence_ids=['ev_safe'], result_summary={'evidence_count': 1})
    draft = {'answer': 'private answer', 'claims': [], 'citations': [], 'notices': [{'code': 'N', 'message': 'safe'}]}
    assert await native(fixture_publish_result, run_id, claimed['lease_token'], draft, {}, False, 'model_finished')
    fixed = datetime.now(timezone.utc) - timedelta(minutes=1)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE agent.model_calls SET started_at=%s WHERE id=%s', (fixed, model_id))
        conn.execute('UPDATE agent.tool_calls SET started_at=%s WHERE id=%s', (fixed, tool_id))
    payload = await native(timeline, run_id)
    parsed = RunTimeline.model_validate(payload)
    types_seen = [event.type for event in parsed.events]
    assert types_seen.index('model_call') < types_seen.index('tool_call')
    tool = next((event for event in parsed.events if event.type == 'tool_call'))
    assert tool.parent_id == str(model_id)
    assert tool.details['retrieval_id'] == 'ret_safe'
    assert tool.details['evidence_ids'] == ['ev_safe']
    published = next((event for event in parsed.events if event.type == 'result_published'))
    assert published.details == {'claim_count': 0, 'citation_count': 0, 'notice_count': 1}
    encoded = json.dumps(payload, default=str)
    assert 'private task' not in encoded
    assert 'private answer' not in encoded
    legacy_id = _queued_run()
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET status='cancelled',finished_at=now(),termination_reason='cancelled_before_start' WHERE id=%s", (legacy_id,))
    assert (await native(timeline, legacy_id))['manifest'] is None

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_rag_search_audit_hashes_results_and_errors(monkeypatch):
    query = '原始 query'
    rewritten = 'rewritten query'
    chunk_ids = [uuid4(), uuid4()]
    rows = [{'id': item, 'document_id': uuid4(), 'version_id': uuid4(), 'content': f'content-{index}', 'source_locator': {'kind': 'txt', 'line_start': 1, 'line_end': 1}, 'title': f'title-{index}'} for index, item in enumerate(chunk_ids)]
    monkeypatch.setattr(retrieval, 'rewrite', lambda value: rewritten)
    monkeypatch.setattr(retrieval, 'embed', lambda *args: [[0.1] * 1024])
    monkeypatch.setattr(retrieval.retrieval_store, '_dense', lambda *args: [dict(row, distance=0.1 * index) for index, row in enumerate(rows)])
    monkeypatch.setattr(retrieval.retrieval_store, '_sparse', lambda *args: [])
    monkeypatch.setattr(retrieval, 'rerank', lambda _query, _contents: [{'index': 1, 'score': 0.9}, {'index': 0, 'score': 0.1}])
    response = await native(retrieval.search, retrieval.EvidenceSearchRequest(query=query, top_k=2), 'run_audit', 'tool_audit')
    expected_ids = ['ev_' + str(chunk_ids[1]), 'ev_' + str(chunk_ids[0])]
    assert [item['evidence_id'] for item in response['evidences']] == expected_ids
    with connect('RAG_DATABASE_URL') as conn:
        row = conn.execute('SELECT * FROM rag.retrieval_audit WHERE retrieval_id=%s', (response['retrieval_id'],)).fetchone()
    assert row['operation'] == 'search'
    assert row['query_sha256'] == hashlib.sha256(query.encode()).hexdigest()
    assert row['rewritten_query_sha256'] == hashlib.sha256(rewritten.encode()).hexdigest()
    assert row['selected_evidence_ids'] == expected_ids
    assert row['candidate_trace']['version'] == 2
    assert row['candidate_trace']['stages']['rerank']['candidates'][0] == {'evidence_id': expected_ids[0], 'rank': 1, 'score': 0.9}
    assert query not in json.dumps(dict(row), default=str)
    error_run, error_tool = ('run_' + uuid4().hex, 'tool_' + uuid4().hex)
    monkeypatch.setattr(retrieval, 'embed', lambda *args: (_ for _ in ()).throw(ModelFailure('EMBEDDING_NOT_CONFIGURED', False)))
    with pytest.raises(ApiError):
        await native(retrieval.search, retrieval.EvidenceSearchRequest(query='failure'), error_run, error_tool)
    with connect('RAG_DATABASE_URL') as conn:
        failed = conn.execute('SELECT operation,selected_evidence_ids,error_code,candidate_trace FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s', (error_run, error_tool)).fetchone()
    assert failed['operation'] == 'search'
    assert failed['selected_evidence_ids'] == []
    assert failed['error_code'] == 'EMBEDDING_NOT_CONFIGURED'
    assert failed['candidate_trace']['stages']['dense']['status'] == 'unavailable'
    assert failed['candidate_trace']['stages']['fts']['status'] in {'not_executed','success'}

@pytest.mark.skipif(not HAS_DATABASE, reason='isolated PostgreSQL not configured')
async def test_rag_evidence_read_audit_success_and_not_found():
    document_id, version_id, chunk_id = (uuid4(), uuid4(), uuid4())
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'Audit read')", (document_id,))
        conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status)\n            VALUES (%s,%s,1,%s,'/unused/audit-read.txt','text/plain','active')", (version_id, document_id, bytes(32)))
        conn.execute('UPDATE rag.documents SET active_version_id=%s WHERE id=%s', (version_id, document_id))
        conn.execute("INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,source_locator,search_text)\n            VALUES (%s,%s,%s,1,'audited content',%s,'audited content')", (chunk_id, document_id, version_id, json.dumps({'kind': 'txt', 'line_start': 1, 'line_end': 1})))
    run_id, tool_id = ('run_' + uuid4().hex, 'tool_' + uuid4().hex)
    evidence_id = 'ev_' + str(chunk_id)
    assert (await native(retrieval.read_evidence, evidence_id, run_id, tool_id))['evidence_id'] == evidence_id
    missing_run, missing_tool = ('run_' + uuid4().hex, 'tool_' + uuid4().hex)
    with pytest.raises(ApiError):
        await native(retrieval.read_evidence, 'ev_' + str(uuid4()), missing_run, missing_tool)
    with connect('RAG_DATABASE_URL') as conn:
        success = conn.execute('SELECT operation,retrieval_id,query_sha256,rewritten_query_sha256,selected_evidence_ids,error_code,candidate_trace FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s', (run_id, tool_id)).fetchone()
        missing = conn.execute('SELECT operation,selected_evidence_ids,error_code,candidate_trace FROM rag.retrieval_audit WHERE run_id=%s AND tool_call_id=%s', (missing_run, missing_tool)).fetchone()
    assert success == {'operation': 'read', 'retrieval_id': None, 'query_sha256': None, 'rewritten_query_sha256': None, 'selected_evidence_ids': [evidence_id], 'error_code': None, 'candidate_trace': None}
    assert missing == {'operation': 'read', 'selected_evidence_ids': [], 'error_code': 'EVIDENCE_NOT_FOUND', 'candidate_trace': None}
    with connect('RAG_DATABASE_URL') as conn:
        conn.execute('UPDATE rag.documents SET active_version_id=NULL WHERE id=%s', (document_id,))
        conn.execute('DELETE FROM rag.chunks WHERE document_id=%s', (document_id,))
        conn.execute('DELETE FROM rag.document_versions WHERE id=%s', (version_id,))
        conn.execute('DELETE FROM rag.documents WHERE id=%s', (document_id,))
