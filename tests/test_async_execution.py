from tests.acceptance_fixtures import fixture_publish_result
"""Real PostgreSQL fault boundaries for protocol 2 and native async execution."""
import asyncio
import os
from uuid import uuid4
from typing import TypedDict
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph,START,END
from langgraph.checkpoint.base import empty_checkpoint
from agent_service import runtime
from agent_service.checkpoints import ProtectedSaver
from agent_service.storage.receipts import MARKER,marker
from execution.contracts import TaskRef,TaskKind
from execution.persistence import transaction,Applied,AlreadyApplied,Rejected,resources,bind
from execution.infra.types import OperationDeadline,WorkOwner
from execution import store
from tests.helpers import insert_run

pytestmark=pytest.mark.skipif(not os.environ.get('AGENT_DATABASE_URL'),reason='isolated PostgreSQL not configured')


async def settled_run():
    rid=insert_run();claim=await runtime.claim_run()
    call_id=await runtime.reserve_model(rid,claim['lease_token'],'react','mock')
    assert isinstance(await runtime.settle_model(rid,claim['lease_token'],call_id,'succeeded'),Applied)
    return rid,claim['lease_token'],call_id


async def recoverable(rid):
    async with transaction(readonly=True) as conn:return await runtime.safe_to_resume(conn,rid)


async def test_reservation_and_settlement_use_stable_identity_once():
    rid=insert_run();claim=await runtime.claim_run();token=claim['lease_token'];call_id=uuid4()
    one=await runtime.reserve_model(rid,token,'react','mock',call_id=call_id)
    two=await runtime.reserve_model(rid,token,'react','mock',call_id=call_id)
    assert one==two==call_id
    assert isinstance(await runtime.settle_model(rid,token,call_id,'succeeded'),Applied)
    assert isinstance(await runtime.settle_model(rid,token,call_id,'succeeded'),AlreadyApplied)
    with pytest.raises(ValueError):await runtime.settle_model(rid,token,call_id,'failed',error_code='different')
    async with transaction(readonly=True) as conn:
        row=await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s',(rid,))).fetchone()
    assert row['model_calls_used']==1
    assert not await recoverable(rid)


async def test_pending_receipt_proves_output_and_detects_corruption():
    rid,token,call_id=await settled_run();saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    cp=empty_checkpoint();cp['id']='0001'
    config=await saver.aput({'configurable':{'thread_id':str(rid),'checkpoint_ns':''}},cp,{}, {})
    message=AIMessage(content='persisted',additional_kwargs={MARKER:marker('model',call_id)})
    assert not await recoverable(rid)
    await saver.aput_writes(config,[('messages',[message])],'node')
    assert await recoverable(rid)
    async with transaction() as conn:
        # Runtime can write graph records, but can never alter the immutable receipt.
        await conn.execute("UPDATE agent.checkpoint_writes SET blob=%s WHERE thread_id=%s",(b'corrupt',str(rid)))
    assert not await recoverable(rid)


async def test_conflicting_pending_write_does_not_create_receipt():
    rid,token,call_id=await settled_run();saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    cp=empty_checkpoint();cp['id']='0001'
    config=await saver.aput({'configurable':{'thread_id':str(rid),'checkpoint_ns':''}},cp,{}, {})
    await saver.aput_writes(config,[('messages',['old'])],'node')
    message=AIMessage(content='new',additional_kwargs={MARKER:marker('model',call_id)})
    with pytest.raises(ValueError,match='conflict'):await saver.aput_writes(config,[('messages',[message])],'node')
    async with transaction(readonly=True) as conn:
        assert not await (await conn.execute('SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s',(rid,))).fetchone()


async def test_full_checkpoint_and_receipt_share_guarded_transaction():
    rid,token,call_id=await settled_run();saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    cp=empty_checkpoint();cp['id']='0001';cp['channel_values']={'messages':[AIMessage(content='saved',additional_kwargs={MARKER:marker('model',call_id)})]};cp['channel_versions']={'messages':'1'}
    config={'configurable':{'thread_id':str(rid),'checkpoint_ns':''}}
    await saver.aput(config,cp,{}, {'messages':'1'})
    assert await recoverable(rid)
    async with transaction() as conn:await conn.execute('UPDATE execution.tasks SET lease_token=%s WHERE id=%s',(uuid4(),rid))
    cp['id']='0002';cp['channel_versions']={'messages':'2'}
    with pytest.raises(PermissionError):await saver.aput(config,cp,{}, {'messages':'2'})
    async with transaction(readonly=True) as conn:
        assert not await (await conn.execute("SELECT 1 FROM agent.checkpoints WHERE thread_id=%s AND checkpoint_id='0002'",(str(rid),))).fetchone()
        assert not await (await conn.execute("SELECT 1 FROM agent.checkpoint_blobs WHERE thread_id=%s AND version='2'",(str(rid),))).fetchone()
        assert not await (await conn.execute("SELECT 1 FROM agent.call_checkpoint_receipts WHERE run_id=%s AND checkpoint_id='0002'",(rid,))).fetchone()


async def test_graph_resume_reuses_successful_pending_node():
    rid=insert_run();claim=await runtime.claim_run();token=claim['lease_token'];calls=[];cut=True
    persisted=asyncio.Event()
    class FaultSaver(ProtectedSaver):
        async def aput_writes(self,config,writes,task_id,task_path=''):
            await super().aput_writes(config,writes,task_id,task_path)
            if any(channel=='messages' and value and isinstance(value[0],AIMessage) for channel,value in writes):persisted.set()
    class State(TypedDict):
        messages:list
    async def completed(state):
        call_id=await runtime.reserve_model(rid,token,'react','mock');calls.append(call_id)
        await runtime.settle_model(rid,token,call_id,'succeeded')
        return {'messages':[AIMessage(content='durable',additional_kwargs={MARKER:marker('model',call_id)})]}
    async def interrupted(state):
        if cut:
            await persisted.wait()
            raise RuntimeError('cut after sibling succeeded')
        return {}
    graph=StateGraph(State);graph.add_node('completed',completed);graph.add_node('interrupted',interrupted)
    graph.add_edge(START,'completed');graph.add_edge(START,'interrupted');graph.add_edge('completed',END);graph.add_edge('interrupted',END)
    compiled=graph.compile(checkpointer=FaultSaver(TaskRef(rid,TaskKind.AGENT),token))
    config={'configurable':{'thread_id':str(rid)}}
    with pytest.raises(RuntimeError):await compiled.ainvoke({'messages':[]},config,durability='sync')
    assert len(calls)==1 and await recoverable(rid)
    cut=False
    result=await compiled.ainvoke(None,config,durability='sync')
    assert len(calls)==1 and result['messages'][0].content=='durable'


async def test_publication_duplicate_reconciles_without_regeneration():
    rid=insert_run();claim=await runtime.claim_run();token=claim['lease_token']
    draft={'answer':'once','claims':[],'citations':[],'notices':[]}
    result_id=uuid4()
    first=await fixture_publish_result(rid,token,draft,{},False,'done',result_id=result_id)
    second=await fixture_publish_result(rid,token,draft,{},False,'done',result_id=result_id)
    assert isinstance(first,Applied) and isinstance(second,AlreadyApplied)
    assert first.value==second.value==result_id
    with pytest.raises(ValueError):await fixture_publish_result(rid,token,{**draft,'answer':'different'},{},False,'done',result_id=result_id)


async def test_full_slots_still_finalize_expired_cancellation(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS','1')
    rid=insert_run();claim=await runtime.claim_run()
    async with transaction(profile='control') as conn:
        await store.stop(conn,TaskRef(rid,TaskKind.AGENT))
        await conn.execute("UPDATE execution.tasks SET leased_until=clock_timestamp()-interval '1 second' WHERE id=%s",(rid,))
    successor=insert_run()
    next_claim=await runtime.claim_run()
    assert next_claim['run_id']==successor
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT status FROM execution.tasks WHERE id=%s',(rid,))).fetchone())['status']=='cancelled'


async def test_quiesce_admits_control_cleanup_but_rejects_new_work():
    rid=insert_run();claim=await runtime.claim_run();local=resources();shutdown=local.begin_shutdown()
    with bind(local,deadline=shutdown,cleanup=True):
        await runtime.attempt_exited(rid,claim['lease_token'])
        with pytest.raises(Exception):await runtime.reserve_model(rid,claim['lease_token'],'react','mock')
    assert local._closing_deadline is shutdown


@pytest.mark.parametrize('cut',['disconnect','cancel'])
async def test_lost_commit_ack_is_unknown_and_never_replays_reservation(monkeypatch,cut):
    import psycopg
    from psycopg.transaction import AsyncTransaction
    from execution.persistence import PersistenceOutcomeUnknown
    rid=insert_run();claim=await runtime.claim_run();call_id=uuid4();original=AsyncTransaction.__aexit__
    fired=False
    async def lose_ack(self,exc_type,exc_val,exc_tb):
        nonlocal fired
        result=await original(self,exc_type,exc_val,exc_tb)
        if exc_type is None and not fired:
            fired=True
            if cut=='cancel':raise asyncio.CancelledError()
            raise psycopg.OperationalError('commit acknowledgement lost')
        return result
    monkeypatch.setattr(AsyncTransaction,'__aexit__',lose_ack)
    with pytest.raises(PersistenceOutcomeUnknown):
        await runtime.reserve_model(rid,claim['lease_token'],'react','mock',call_id=call_id)
    # The reservation exists, but no external request may be inferred or dispatched.
    async with transaction(readonly=True) as conn:
        row=await (await conn.execute('SELECT status FROM agent.model_calls WHERE id=%s',(call_id,))).fetchone()
        assert row['status']=='started'
    await runtime.attempt_exited(rid,claim['lease_token'])
    async with transaction(readonly=True) as conn:
        task=await (await conn.execute('SELECT status,error_code FROM execution.tasks WHERE id=%s',(rid,))).fetchone()
    assert task=={'status':'failed','error_code':'INTERRUPTED_UNKNOWN'}


async def test_sdk_transport_shares_capacity_and_closes_without_sync_client():
    import httpx
    from agent_service.model_adapter import model
    from execution.persistence import call_scope
    from scripts.mock_model_service import completion
    seen=[]
    def response(request):
        seen.append(request)
        assert b'_execution_call' not in request.content
        return httpx.Response(200,json=completion({'content':'native async'}))
    resources().http.transports['agent-model']=httpx.MockTransport(response)
    with call_scope(OperationDeadline.after(2)):
        result=await model().ainvoke([('user','hello')])
    assert result.content=='native async' and len(seen)==1
    assert resources().http.gates['agent-model'].active==0
