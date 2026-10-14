"""Real-DB acceptance failures and authorization races with scripted judgments."""
import asyncio
from uuid import uuid4
import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from agent_service import execution,runtime,model_calls
from agent_service.schemas import StructuredSummary,StepEvaluation
from agent_service.storage.runs import get_run,create_run
from contracts.v1 import RunCreate,ToolAuthorization
from execution.call_store import AgentCallStore
from execution.persistence import resources,transaction,PersistenceOutcomeUnknown
from tests.helpers import insert_run
from tests.test_agent_runtime import ScriptedModel,EvidenceModel,FakeRagClient
from tests.test_acceptance import configure_provider

@pytest.mark.parametrize('mode',['react','plan_execute'])
@pytest.mark.parametrize('fault',['step_incomplete','final_incomplete','conditions_dropped','contract_omission','final_timeout'])
async def test_independent_failure_always_publishes_partial(monkeypatch,mode,fault):
    class Judge(ScriptedModel):
        def quality_response(self,name,data,value):
            if name=='StepEvaluation' and fault=='step_incomplete':value['status']='not_met'
            if name=='GoalContract' and fault=='conditions_dropped':value['requirements'][0]['conditions']=['Research records only']
            if name=='FinalEvaluation':
                if fault=='final_incomplete':value['requirements'][0]['status']='not_met'
                if fault=='contract_omission':value['original_task_covered']=False
                if fault=='final_timeout':raise openai.APITimeoutError(request=httpx.Request('POST','http://synthetic.local'))
            return value
    monkeypatch.setattr(execution,'model',lambda:Judge(responses=[AIMessage(content='Executor says completed')]))
    rid=insert_run(mode=mode);job=await runtime.claim_run()
    await execution.execute_run(rid,job['lease_token'])
    result=await get_run(rid)
    assert result['status']=='partial' and result['result']['acceptance']['status']=='partial'
    async with transaction(readonly=True) as conn:
        calls=await (await conn.execute("SELECT purpose,count(*) AS n FROM agent.model_calls WHERE run_id=%s GROUP BY purpose",(rid,))).fetchall()
        assert all(c['n']==1 for c in calls if c['purpose'] in ('final','final_acceptance'))
        if mode=='plan_execute' and fault in ('step_incomplete','conditions_dropped'):
            assert not await (await conn.execute("SELECT 1 FROM agent.run_steps WHERE run_id=%s AND status='completed'",(rid,))).fetchone()

async def test_real_citation_cannot_support_rejected_semantics(monkeypatch):
    from agent_service.tools import rag
    class Judge(EvidenceModel):
        def quality_response(self,name,data,value):
            if name=='FinalEvaluation':value['claims'][0]['status']='contradicted'
            return value
    messages=[AIMessage(content='',tool_calls=[dict(name='search_evidence',args={'query':'policy'},id='search')]),AIMessage(content='Completed')]
    monkeypatch.setattr(execution,'model',lambda:Judge(responses=messages))
    monkeypatch.setattr(rag,'RagClient',FakeRagClient)
    monkeypatch.setenv('RAG_BASE_URL','http://synthetic.local');monkeypatch.setenv('RAG_SERVICE_TOKEN','synthetic')
    rid=insert_run();job=await runtime.claim_run();await execution.execute_run(rid,job['lease_token'])
    result=await get_run(rid)
    assert result['status']=='partial' and not result['result']['citations']
    assert 'Seven years' not in result['result']['answer']
    assert result['result']['acceptance']['claims'][0]['status']=='contradicted'

async def test_authorization_capacity_and_stable_identity_are_atomic(monkeypatch):
    configure_provider(monkeypatch)
    await create_run(RunCreate(task='Append research notes',tool_authorizations=[ToolAuthorization(tool_name='synthetic_write',operation='append',exact_arguments={'target':'research'})]),'capacity')
    job=await runtime.claim_run();rid,token=job['run_id'],job['lease_token']
    async with transaction(readonly=True) as conn:
        grant=await (await conn.execute('SELECT id FROM agent.run_tool_authorizations WHERE run_id=%s',(rid,))).fetchone()
    ledger=AgentCallStore(resources());ids=[uuid4(),uuid4()]
    arguments={'authorization_id':str(grant['id']),'effect_arguments':{'target':'research'},'arguments_sha256':'fixture'}
    result=await asyncio.gather(*(ledger.reserve_tool(rid,token,'synthetic_write',arguments,call_id=i) for i in ids),return_exceptions=True)
    assert sum(isinstance(r,ValueError) for r in result)==1
    winner=ids[next(i for i,r in enumerate(result) if not isinstance(r,BaseException))]
    assert await ledger.reserve_tool(rid,token,'synthetic_write',arguments,call_id=winner)
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT count(*) AS n FROM agent.tool_authorization_uses WHERE run_id=%s',(rid,))).fetchone())['n']==1
        assert (await (await conn.execute('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s',(rid,))).fetchone())['tool_calls_used']==1

async def test_final_checker_commit_unknown_never_becomes_partial(monkeypatch):
    monkeypatch.setattr(execution,'model',lambda:ScriptedModel(responses=[AIMessage(content='Completed')]))
    settle=model_calls.settle_model
    async def lost(run_id,token,identity,status,**metadata):
        outcome=await settle(run_id,token,identity,status,**metadata)
        if (metadata.get('output_summary') or {}).get('schema')=='FinalEvaluation':
            raise PersistenceOutcomeUnknown('Synthetic acknowledgement lost')
        return outcome
    monkeypatch.setattr(model_calls,'settle_model',lost)
    rid=insert_run();job=await runtime.claim_run()
    with pytest.raises(PersistenceOutcomeUnknown):await execution.execute_run(rid,job['lease_token'])
    assert (await get_run(rid))['result'] is None
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM agent.model_calls WHERE run_id=%s AND purpose='final'",(rid,))).fetchone())['n']==1

async def test_completed_step_requires_real_checkpoint_receipt():
    from agent_service.storage import graph
    from agent_service.acceptance import digest
    from psycopg.types.json import Jsonb
    rid=insert_run();job=await runtime.claim_run();token=job['lease_token']
    contract={'requirements':[dict(id='r1',description='test task',completion_condition='Done',conditions=[])]}
    await graph.prepare_steps(rid,token,[dict(goal='Deliver',completion_condition='Done',requirement_ids=['r1'])])
    step=await graph.start_step(rid,token,1)
    evaluation=StepEvaluation(status='met',reason='Done',summary=StructuredSummary(outputs=['Done'])).model_dump()
    cid=uuid4()
    async with transaction() as conn:
        await conn.execute("INSERT INTO agent.model_calls(id,run_id,step_id,purpose,model,status,output_summary,input_summary,attempt_token) VALUES (%s,%s,%s,'step_acceptance','fixture','succeeded',%s,%s,%s)",(cid,rid,step['id'],Jsonb(dict(schema='StepEvaluation',payload_sha256=digest(evaluation))),Jsonb(dict(acceptance_input_sha256=digest(dict(task='test task',evidence={},contract=contract,step_id=str(step['id']),completion_condition='Done')))),token))
    with pytest.raises(ValueError,match='checkpoint receipt'):
        await graph.complete_step(rid,token,step['id'],evaluation,cid,'test task',{},contract)
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT status FROM agent.run_steps WHERE id=%s',(step['id'],))).fetchone())['status']=='running'

async def test_persisted_tool_judgment_is_reused_after_interruption(monkeypatch):
    from agent_service.checkpoints import ProtectedSaver
    from agent_service.tooling import RunExecutionState
    from execution.contracts import TaskRef,TaskKind
    from tests.test_agent_runtime import configure_extra_tools,seen_tool_context
    configure_extra_tools(monkeypatch);seen_tool_context.clear()
    model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[dict(name='echo_value',args={'value':'research'},id='echo')]),AIMessage(content='Echo completed')])
    monkeypatch.setattr(execution,'model',lambda:model)
    rid=insert_run();job=await runtime.claim_run();token=job['lease_token']
    from agent_service.storage import acceptance as acceptance_store
    confirm=acceptance_store.confirm_intents
    cut=True
    async def interrupt_after_confirmation(*args,**kwargs):
        nonlocal cut
        result=await confirm(*args,**kwargs)
        if cut and kwargs.get('dispatch'):
            cut=False
            raise RuntimeError('Synthetic interruption after durable judgment')
        return result
    monkeypatch.setattr(acceptance_store,'confirm_intents',interrupt_after_confirmation)
    saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    graph=execution.react_agent(saver);config={'configurable':{'thread_id':str(rid)}}
    with pytest.raises(RuntimeError,match='durable judgment'):
        await graph.ainvoke({'messages':[{'role':'user','content':'Echo research'}]},config,context=RunExecutionState(rid,token),durability='sync')
    assert not seen_tool_context
    async with transaction(readonly=True) as conn: assert await runtime.safe_to_resume(conn,rid)
    output=await graph.ainvoke(None,config,context=RunExecutionState(rid,token),durability='sync')
    assert output['step_evaluation']['status']=='met' and seen_tool_context==[rid]
    async with transaction(readonly=True) as conn:
        for purpose in ('goal_analysis','tool_intent','step_acceptance'):
            assert (await (await conn.execute('SELECT count(*) AS n FROM agent.model_calls WHERE run_id=%s AND purpose=%s',(rid,purpose))).fetchone())['n']==1

async def test_two_final_slots_are_reserved_and_final_phases_do_not_replay():
    from agent_service.runtime import BudgetExhausted
    rid=insert_run();job=await runtime.claim_run();token=job['lease_token']
    for i in range(14):await runtime.reserve_model(rid,token,'react','fixture')
    with pytest.raises(BudgetExhausted):await runtime.reserve_model(rid,token,'react','fixture')
    final=await runtime.reserve_model(rid,token,'final','fixture',final=True)
    await runtime.settle_model(rid,token,final,'succeeded')
    check=await runtime.reserve_model(rid,token,'final_acceptance','fixture',final=True)
    await runtime.settle_model(rid,token,check,'succeeded')
    with pytest.raises(ValueError,match='replayed'):await runtime.reserve_model(rid,token,'final_acceptance','fixture',final=True)
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s',(rid,))).fetchone())['model_calls_used']==16

async def test_tool_intent_receives_prior_results_as_data(monkeypatch):
    from tests.test_agent_runtime import configure_extra_tools
    from agent_service.tools import rag
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv('AGENT_TOOL_PROVIDERS','agent_service.tools.rag:tools,test_extra_tool_provider:tools')
    monkeypatch.setenv('RAG_BASE_URL','http://synthetic.local');monkeypatch.setenv('RAG_SERVICE_TOKEN','synthetic')
    monkeypatch.setattr(rag,'RagClient',FakeRagClient)
    seen=[]
    class Judge(ScriptedModel):
        def quality_response(self,name,data,value):
            if name=='ToolBatchEvaluation':seen.append(data)
            return value
    model=Judge(responses=[AIMessage(content='',tool_calls=[dict(name='search_evidence',args={'query':'policy'},id='search')]),
        AIMessage(content='',tool_calls=[dict(name='echo_value',args={'value':'ev-tool'},id='echo')]),AIMessage(content='Completed')])
    monkeypatch.setattr(execution,'model',lambda:model)
    rid=insert_run(task='Find the policy and report its evidence ID');job=await runtime.claim_run()
    await execution.execute_run(rid,job['lease_token'])
    assert (await get_run(rid))['status']=='completed' and len(seen)==2
    assert not seen[0]['known_evidence']
    assert seen[1]['known_evidence'][0]['evidence_id']=='ev-tool'
    assert seen[1]['observations_lower_trust'][0]['tool_call_id']=='search'
    assert all(d['original_task']=='Find the policy and report its evidence ID' for d in seen)
