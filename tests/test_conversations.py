from tests.native_async import native_model
from tests.native_async import native, native_runnable
"""Personal conversation facts, historical context and atomic Memory completion."""
import os
from uuid import UUID, uuid4
import pytest
import psycopg
from langchain_core.runnables import RunnableLambda
from agent_service.app import app
from agent_service import memory
from agent_service.runtime import claim_run, publish_result
from tests.test_phase1 import local
from db.connection import connect
from execution import store
from execution.contracts import TaskRef, TaskKind
from execution.inputs import enqueue_memory
from execution.scope import attempt
pytestmark = pytest.mark.skipif(not os.environ.get('AGENT_DATABASE_URL'), reason='isolated PostgreSQL not configured')

def turn(client, cid, text, key='one'):
    return client.post(f'/v1/conversations/{cid}/turns', headers={'Idempotency-Key': key}, json={'task': text})

def conversation_turn(text='Project A uses MySQL'):
    with local(app) as client:
        cid = client.post('/v1/conversations').json()['conversation_id']
        payload = turn(client, cid, text).json()
    return (UUID(cid), UUID(payload['turn_id']), UUID(payload['run_id']))

def terminal(run_id, answer='Done'):
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET status='completed',finished_at=clock_timestamp() WHERE id=%s", (run_id,))
        conn.execute('INSERT INTO agent.run_results(id,run_id,answer) VALUES (%s,%s,%s)', (uuid4(), run_id, answer))

def test_conversation_idempotency_busy_and_cancel():
    with local(app) as client:
        cid = client.post('/v1/conversations').json()['conversation_id']
        assert client.get(f'/v1/conversations/{cid}').status_code == 200
        assert turn(client, cid, 'Project A uses MySQL', '').status_code == 422
        first = turn(client, cid, 'Project A uses MySQL')
        assert first.status_code == 202
        assert turn(client, cid, 'Project A uses MySQL').json()['run_id'] == first.json()['run_id']
        assert turn(client, cid, 'different').status_code == 409
        assert turn(client, cid, 'new', 'two').json()['error']['code'] == 'CONVERSATION_BUSY'
        assert client.post(f'/v1/conversations/{cid}/turns/{first.json()['turn_id']}/cancel').status_code == 200
        second = turn(client, cid, 'Project A uses PostgreSQL', 'two')
        assert second.status_code == 202 and second.json()['ordinal'] == 2
        turns = client.get(f'/v1/conversations/{cid}').json()['turns']
        assert turns[0]['status'] == 'cancelled' and turns[0]['answer'] is None
        assert client.get(f'/v1/conversations/{uuid4()}').status_code == 404
        other = client.post('/v1/conversations').json()['conversation_id']
        assert turn(client, other, 'other', 'one').status_code == 202

async def test_snapshot_is_immutable_and_database_failure_is_not_embedding_degradation(monkeypatch):
    cid, tid, rid = conversation_turn()
    terminal(rid, 'Acknowledged')
    with local(app) as client:
        second = turn(client, str(cid), 'Which database?', 'two').json()
    rid = UUID(second['run_id'])
    claimed = await native(claim_run)
    monkeypatch.setattr(memory, 'embedding', lambda _: (_ for _ in ()).throw(memory.ProviderFailure('EMBEDDING_UNAVAILABLE')))
    snapshot = await native(memory.capture_snapshot, rid, claimed['lease_token'], 'Which database?')
    assert snapshot['memory_degraded'] and snapshot['history'][0]['assistant'] == 'Acknowledged'
    assert await native(memory.capture_snapshot, rid, claimed['lease_token'], 'different') == snapshot
    with local(app) as client:
        client.post('/v1/runs/' + str(rid) + '/cancel')
    with pytest.raises(PermissionError):
        await native(memory.capture_snapshot, rid, claimed['lease_token'], 'x')

@pytest.mark.parametrize('valid', [True, False])
async def test_fact_provenance_and_atomic_completion(monkeypatch, valid):
    cid, tid, rid = conversation_turn()
    job = await native(memory.claim_job)
    assert job['turn_id'] == tid

    @native_model
    class Model:

        def with_structured_output(self, schema):
            return native_runnable(lambda _: memory.FactList(facts=[memory.FactDraft(statement='Project A uses MySQL', source_quote='uses MySQL' if valid else 'uses PostgreSQL', subject='Project A', subject_quote='Project A', subject_turn_id=tid)]))
    monkeypatch.setattr(memory, 'model', lambda: Model())
    monkeypatch.setattr(memory, 'embedding', lambda _: ('mock', [[0.1, 0.2]]))
    with attempt(memory.job_ref(job), job['lease_token']):
        await native(memory.extract, job)
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT count(*) AS n FROM agent.personal_facts').fetchone()['n'] == int(valid)
        assert conn.execute('SELECT status FROM execution.tasks WHERE id=%s', (job['id'],)).fetchone()['status'] == 'completed'
    with local(app) as client:
        assert len(client.get('/v1/memories').json()['items']) == int(valid)

@pytest.mark.parametrize('revoked', ['lease', 'deadline', 'token'])
async def test_late_memory_facts_and_summary_are_rejected(monkeypatch, revoked):
    cid, tid, rid = conversation_turn()
    terminal(rid)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET status='failed' WHERE kind='memory_job'")
        await native(enqueue_memory, conn, tid, 'summary')
    job = await native(memory.claim_job)

    @native_model
    class Model:

        def with_structured_output(self, schema):

            def generate(_):
                with connect('AGENT_DATABASE_URL') as conn:
                    if revoked == 'token':
                        conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s', (uuid4(), job['id']))
                    else:
                        conn.execute(f"UPDATE execution.tasks SET {('leased_until' if revoked == 'lease' else 'execution_deadline_at')}=clock_timestamp()-interval '1 second' WHERE id=%s", (job['id'],))
                return memory.SummaryDraft(summary='late summary')
            return native_runnable(generate)
    monkeypatch.setattr(memory, 'model', lambda: Model())
    with pytest.raises(PermissionError):
        await native(memory.summarize, job)
    with connect('AGENT_DATABASE_URL') as conn:
        assert not conn.execute('SELECT 1 FROM agent.conversation_summaries').fetchone()

async def test_memory_three_attempts_and_deadline_snapshot():
    cid, tid, rid = conversation_turn()
    jobs = []
    for i in range(3):
        job = await native(memory.claim_job)
        jobs.append(job)
        assert job['attempts'] == i + 1
        assert await native(memory.finish_job, job, 'MODEL_CALL_FAILED')
    assert len({j['deadline_at'] for j in jobs}) == 1
    assert await native(memory.claim_job) is None
    with connect('AGENT_DATABASE_URL') as conn:
        state = conn.execute('SELECT status,attempts,execution_seconds FROM execution.tasks WHERE id=%s', (jobs[0]['id'],)).fetchone()
    assert state == {'status': 'failed', 'attempts': 3, 'execution_seconds': 900}

async def test_covered_summary_finishes_without_model(monkeypatch):
    cid, tid, rid = conversation_turn()
    terminal(rid)
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET status='failed' WHERE kind='memory_job'")
        await native(enqueue_memory, conn, tid, 'summary')
        conn.execute('INSERT INTO agent.conversation_summaries(id,conversation_id,version,through_ordinal,content) VALUES (%s,%s,1,1,%s)', (uuid4(), cid, 'already covered'))
    job = await native(memory.claim_job)
    monkeypatch.setattr(memory, 'model', lambda: pytest.fail('Covered summary must not call the model'))
    await native(memory.summarize, job)
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT status FROM execution.tasks WHERE id=%s', (job['id'],)).fetchone()['status'] == 'completed'

@pytest.mark.parametrize('statement,quote,allowed', [('Project A migrated to PostgreSQL', 'uses PostgreSQL', False), ('Project A currently uses PostgreSQL', 'uses PostgreSQL', False), ('Project A uses PostgreSQL', 'uses PostgreSQL', True)])
async def test_memory_does_not_invent_change_or_current_state(monkeypatch, statement, quote, allowed):
    cid, tid, rid = conversation_turn('Project A uses PostgreSQL')
    job = await native(memory.claim_job)

    @native_model
    class Model:

        def with_structured_output(self, schema):
            return native_runnable(lambda _: memory.FactList(facts=[memory.FactDraft(statement=statement, source_quote=quote, subject='Project A', subject_quote='Project A', subject_turn_id=tid)]))
    monkeypatch.setattr(memory, 'model', lambda: Model())
    monkeypatch.setattr(memory, 'embedding', lambda _: ('mock', [[0.1, 0.2]]))
    await native(memory.extract, job)
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT count(*) AS n FROM agent.personal_facts').fetchone()['n'] == int(allowed)

async def test_memory_related_subject_keeps_contradictory_history(monkeypatch):
    from psycopg.types.json import Jsonb
    cid, first_tid, first_rid = conversation_turn('Project A uses MySQL')
    terminal(first_rid)
    with local(app) as client:
        second = turn(client, str(cid), 'Project A uses PostgreSQL', 'two').json()
    second_tid, second_rid = (UUID(second['turn_id']), UUID(second['run_id']))
    terminal(second_rid)
    with connect('AGENT_DATABASE_URL') as conn:
        for tid, text, vector in ((first_tid, 'Project A uses MySQL', '[1,0]'), (second_tid, 'Project A uses PostgreSQL', '[0,1]')):
            conn.execute("INSERT INTO agent.personal_facts(id,source_turn_id,statement,source_quote,subject,subject_quote,subject_turn_id,stated_at,embedding,embedding_model)\n                VALUES (%s,%s,%s,%s,'Project A','Project A',%s,clock_timestamp(),%s::vector,'mock')", (uuid4(), tid, text, text, tid, vector))
    with local(app) as client:
        third = turn(client, str(cid), 'What has Project A used?', 'three').json()
    rid = UUID(third['run_id'])
    claimed = await native(claim_run)
    monkeypatch.setattr(memory, 'embedding', lambda _: ('mock', [[1, 0]]))
    snapshot = await native(memory.capture_snapshot, rid, claimed['lease_token'], 'history')
    assert {f['statement'] for f in snapshot['facts']} == {'Project A uses MySQL', 'Project A uses PostgreSQL'}
    assert len(snapshot['history']) == 2
    with connect('AGENT_DATABASE_URL') as conn:
        for table in ('personal_facts', 'conversation_turns', 'run_memory_snapshots'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(f'DELETE FROM agent.{table}')
