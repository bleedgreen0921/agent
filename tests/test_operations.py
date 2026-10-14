from tests.native_async import native, native_runnable
import os
from datetime import timedelta
from uuid import uuid4
import pytest
from agent_service.app import app
from agent_service import operations
from db.connection import connect
from tests.helpers import insert_run
from tests.test_phase1 import local
pytestmark = pytest.mark.skipif(not os.environ.get('AGENT_DATABASE_URL'), reason='isolated PostgreSQL not configured')

def test_personal_list_pagination_filters_and_cursor_scope():
    ids = [insert_run(created_delta=timedelta(seconds=-i)) for i in range(4)]
    with local(app) as client:
        first = client.get('/v1/runs', params={'limit': 2}).json()
        assert [str(row['id']) for row in first['items']] == list(map(str, ids[:2]))
        second = client.get('/v1/runs', params={'limit': 2, 'cursor': first['next_cursor']}).json()
        assert [str(row['id']) for row in second['items']] == list(map(str, ids[2:]))
        assert second['next_cursor'] is None
        assert client.get('/v1/runs', params={'cursor': first['next_cursor'], 'status': 'failed'}).status_code == 422
        assert client.get('/v1/runs', params={'cursor': 'garbage'}).status_code == 422
        assert client.get('/v1/runs', params={'created_after': '2020-01-01T00:00:00'}).status_code == 422
        assert client.get('/v1/runs', params={'status': 'failed'}).json()['items'] == []
        assert 'team' not in str(first)

async def test_summary_excludes_unsafe_recovery_and_counts_expired():
    unsafe = insert_run(status='running', lease_delta=timedelta(seconds=-1), created_delta=timedelta(minutes=-20))
    safe = insert_run(status='running', lease_delta=timedelta(seconds=60), created_delta=timedelta(minutes=-10))
    from agent_service.runtime import reserve_model,settle_model
    from agent_service.checkpoints import ProtectedSaver
    from execution.contracts import TaskRef,TaskKind
    from langgraph.checkpoint.base import empty_checkpoint
    from langchain_core.messages import AIMessage
    from agent_service.storage.receipts import MARKER,marker
    with connect('AGENT_DATABASE_URL') as conn:
        token=conn.execute('SELECT lease_token FROM execution.tasks WHERE id=%s',(safe,)).fetchone()['lease_token']
        conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status,checkpointed) VALUES (%s,%s,'react','mock','started',false)",(uuid4(),unsafe))
    call_id=await native(reserve_model,safe,token,'react','mock')
    await native(settle_model,safe,token,call_id,'succeeded')
    cp=empty_checkpoint();cp['channel_values']={'messages':[AIMessage(content='saved',additional_kwargs={MARKER:marker('model',call_id)})]};cp['channel_versions']={'messages':'1'}
    await ProtectedSaver(TaskRef(safe,TaskKind.AGENT),token).aput({'configurable':{'thread_id':str(safe),'checkpoint_ns':''}},cp,{}, {'messages':'1'})
    with connect('AGENT_DATABASE_URL') as conn:conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()-interval '1 second' WHERE id=%s",(safe,))
    state = await native(operations.run_queue_summary)
    assert state['running_lease_expired'] == 2
    with connect('AGENT_DATABASE_URL') as conn:
        created = conn.execute('SELECT created_at FROM execution.tasks WHERE id=%s', (safe,)).fetchone()['created_at']
    assert state['oldest_claimable_created_at'] == created
    with local(app) as client:
        assert client.get('/v1/runs/summary').status_code == 200
