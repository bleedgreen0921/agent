"""Explicit pre-existing acceptance fixtures for persistence/control unit tests."""
from uuid import uuid4
from psycopg.types.json import Jsonb
from db.connection import connect
from agent_service.acceptance import normalize_draft,render_verified,digest,VerifiedResult
from tests.quality_stubs import quality_value

async def checked_fixture(run_id,draft,evidence,partial=False):
    if isinstance(draft,VerifiedResult): return draft
    candidate={k:v for k,v in draft.items() if k in ('answer','claims','notices','blocks')}
    if not candidate.get('claims'):
        candidate['blocks']=[dict(id='delivery',kind='deliverable',text=candidate['answer'],claim_ids=[],requirement_ids=['r1'])]
    candidate=normalize_draft(candidate)
    cited=set(e for c in candidate['claims'] for e in c['evidence_ids'])
    if cited != {e['evidence_id'] for e in draft.get('citations',[])}:raise ValueError('Citation list does not match claims')
    with connect('MIGRATION_DATABASE_URL') as conn:
        task=conn.execute('SELECT task FROM agent.agent_runs WHERE id=%s',(run_id,)).fetchone()['task']
        goal=quality_value('GoalContract',{'original_task':task})
        old=conn.execute('SELECT contract FROM agent.run_goal_contracts WHERE run_id=%s',(run_id,)).fetchone()
        if old:goal=old['contract']
        else:
            gid=uuid4()
            conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status,output_summary) VALUES (%s,%s,'goal_analysis','fixture','succeeded',%s)",(gid,run_id,Jsonb(dict(schema='GoalContract',payload_sha256=digest(goal)))))
            conn.execute('INSERT INTO agent.run_goal_contracts(run_id,task_sha256,contract,contract_sha256,model_call_id) VALUES (%s,%s,%s,%s,%s)',(run_id,digest(task),Jsonb(goal),digest(goal),gid))
        inputs=dict(task=task,contract=goal,evidence=evidence,candidate=candidate)
        evaluation=quality_value('FinalEvaluation',inputs)
        prior=conn.execute("SELECT id FROM agent.model_calls WHERE run_id=%s AND purpose='final_acceptance'",(run_id,)).fetchone()
        cid=prior['id'] if prior else uuid4()
        if not prior:
            conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status,input_summary,output_summary) VALUES (%s,%s,'final_acceptance','fixture','succeeded',%s,%s)",(cid,run_id,Jsonb(dict(acceptance_input_sha256=digest(inputs))),Jsonb(dict(schema='FinalEvaluation',payload_sha256=digest(evaluation)))))
    return render_verified(**inputs,evaluation=evaluation,prior_partial=partial,call_id=cid)

async def fixture_publish_result(run_id,token,draft,evidence,partial,reason,**kwargs):
    from agent_service.runtime import publish_result
    checked=await checked_fixture(run_id,draft,evidence,partial)
    return await publish_result(run_id,token,checked,evidence,partial,reason,**kwargs)

async def prepare_tool(request):
    """Install explicit scripted graph state for direct middleware unit tests."""
    from agent_service.tooling import ToolProvider,ToolDeclaration
    from langchain_core.tools import StructuredTool
    from langchain_core.messages import AIMessage
    from execution.persistence import resources
    from agent_service.intent import batch_identity
    context=request.runtime.context
    with connect('MIGRATION_DATABASE_URL') as conn:
        task=conn.execute('SELECT task FROM agent.agent_runs WHERE id=%s',(context.run_id,)).fetchone()['task']
    context.original_task=task
    context.goal_contract=quality_value('GoalContract',{'original_task':task})
    state=request.state
    state['goal_contract']=context.goal_contract
    msg=next((m for m in state.get('messages',[]) if any(c['id']==request.tool_call['id'] for c in getattr(m,'tool_calls',[]))),None)
    if msg is None:
        msg=AIMessage(content='',tool_calls=[{k:request.tool_call[k] for k in ('name','args','id')}])
        state.setdefault('messages',[]).append(msg)
    # The middleware tests intentionally supply their own handler/schema.
    for c in msg.tool_calls:
        if c['id']==request.tool_call['id']: c['args']=request.tool_call['args'].copy()
    identity=uuid4()
    with connect('MIGRATION_DATABASE_URL') as conn:
        conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status) VALUES (%s,%s,'react','fixture','succeeded')",(identity,context.run_id))
    msg.additional_kwargs['_execution_call']={'kind':'model','id':str(identity)}
    key=batch_identity(task,context.goal_contract,msg,msg.tool_calls)
    state.setdefault('tool_intents',{})
    evaluation=quality_value('ToolBatchEvaluation',{'calls':msg.tool_calls})
    evaluation_id=uuid4()
    with connect('MIGRATION_DATABASE_URL') as conn:
        conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status,input_summary,output_summary,attempt_token) VALUES (%s,%s,'tool_intent','fixture','succeeded',%s,%s,%s)",(evaluation_id,context.run_id,Jsonb(dict(acceptance_input_sha256=key)),Jsonb(dict(schema='ToolBatchEvaluation',payload_sha256=digest(evaluation))),context.lease_token))
    from agent_service.checkpoints import ProtectedSaver
    from execution.contracts import TaskRef,TaskKind
    from langgraph.checkpoint.base import empty_checkpoint
    cp=empty_checkpoint();cp['channel_values']={'_execution_calls':[{'kind':'model','id':str(evaluation_id)}],'tool_batch_evaluation':evaluation}
    cp['channel_versions']={'_execution_calls':str(evaluation_id),'tool_batch_evaluation':str(evaluation_id)}
    await ProtectedSaver(TaskRef(context.run_id,TaskKind.AGENT),context.lease_token).aput({'configurable':{'thread_id':str(context.run_id)}},cp,{},cp['channel_versions'])
    state['tool_batch_evaluation']=evaluation
    for c in msg.tool_calls:
        state['tool_intents'][c['id']]=dict(batch_hash=key,arguments_sha256=digest(c['args']),tool_name=c['name'],allowed=True,authorization_id=None,evaluation_call_id=str(evaluation_id))
    runtime=resources()
    from agent_service.tooling import load_providers
    if getattr(runtime,'tool_providers',None) is None:runtime.tool_providers=load_providers()
    names={d.tool.name for _,p in runtime.tool_providers for d in p.tools}
    if request.tool_call['name'] not in names:
        async def unused(**kwargs): raise AssertionError('Fixture supplies explicit handler')
        tool=StructuredTool.from_function(coroutine=unused,name=request.tool_call['name'],description='Synthetic middleware probe',args_schema={'type':'object','properties':{}},response_format='content_and_artifact')
        runtime.tool_providers.append(('fixture:probe',ToolProvider('probe','1',(ToolDeclaration(tool,'1','read_only',False),),1)))
    return request
