"""Token, real dispatch, immutable dependencies and restricted lifecycle races."""
import asyncio
import gzip
import json
import os
from pathlib import Path
from uuid import uuid4
import httpx
import psycopg
import pytest
from psycopg.rows import dict_row
from execution import tokens
from execution.persistence import resources,transaction,PersistenceOutcomeUnknown
from execution.scope import attempt
from execution.contracts import TaskRef,TaskKind
from execution.infra.types import OperationDeadline
from tests.helpers import insert_run


def owner():
    return psycopg.connect(os.environ['MIGRATION_DATABASE_URL'],autocommit=True,row_factory=dict_row)


async def claimed():
    from agent_service.runtime import claim_run
    identity=insert_run();row=await claim_run()
    return identity,row['lease_token']


async def test_request_preflight_includes_schema_and_bounded_output():
    value=await tokens.envelope('agent-model',{'model':'local','messages':[{'role':'user','content':'条件不可省略'}],
        'max_tokens':999999,'response_format':{'schema':{'description':'a'*100}}})
    assert value.body['max_tokens']==4096 and value.estimation=='estimated_bytes'
    bigger=await tokens.envelope('agent-model',{**value.body,'tools':[{'description':'x'*500}]})
    assert bigger.input_tokens>value.input_tokens
    with pytest.raises(tokens.InputTooLarge):await tokens.envelope('agent-model',{'model':'local','messages':[{'role':'user','content':'x'*40000}]})


async def test_concurrent_reservations_and_existing_identity_never_redispatch(monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN_LIMIT','72000')
    resources().token_settings=tokens.configuration()
    rid,token=await claimed()
    value=await tokens.envelope('agent-model',{'model':'local','messages':[]})
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        outcomes=await asyncio.gather(tokens.reserve(uuid4(),value),tokens.reserve(uuid4(),value),return_exceptions=True)
        assert sum(isinstance(x,tokens.TokenBudgetExceeded) for x in outcomes)==1
        handle=next(x for x in outcomes if isinstance(x,tuple))
        with pytest.raises(PersistenceOutcomeUnknown):await tokens.reserve(handle[1],value)
        await tokens.sent(handle)
        with pytest.raises(ValueError):await tokens.settle(handle,not_sent=True)
        await tokens.settle(handle)
    report=await tokens.report(rid)
    assert report['calls']==1 and report['unconfirmed_tokens']==value.input_tokens+value.output_tokens


async def test_unsent_release_and_actual_usage_are_idempotent():
    rid,token=await claimed();value=await tokens.envelope('agent-model',{'model':'local','messages':[]})
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        first=await tokens.reserve(uuid4(),value)
        await tokens.settle(first,not_sent=True)
        second=await tokens.reserve(uuid4(),value);await tokens.sent(second)
        await tokens.settle(second,tokens.UsageRecord(123,7,'provider'))
        await tokens.settle(second,tokens.UsageRecord(123,7,'provider'))
    report=await tokens.report(rid)
    assert report['charged']==130 and report['actual_input_tokens']==123 and report['actual_output_tokens']==7


async def test_wire_archive_omits_credentials_and_retains_bad_structured_usage(monkeypatch,tmp_path):
    monkeypatch.setenv('CALL_ARCHIVE_DIR',str(tmp_path/'archive'))
    runtime=resources();received=[]
    async def handler(request):
        received.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'not json'}}],
            'usage':{'prompt_tokens':31,'completion_tokens':4}})
    runtime.http.transports['agent-model']=httpx.MockTransport(handler)
    rid,token=await claimed()
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        client=runtime.http.sdk_client('agent-model')
        reply=await client.post('http://local.test/v1/chat/completions',json={'model':'local','messages':[{'role':'user','content':'Exact original input'}]},
                                headers={'Authorization':'Bearer credential-must-not-be-archived'})
        assert reply.json()['choices'][0]['message']['content']=='not json'
    assert received[0]['max_tokens']==4096
    rows=list((tmp_path/'archive').glob('*.gz'))
    data=[gzip.decompress(p.read_bytes()).decode() for p in rows]
    assert len(rows)==2 and any('Exact original input' in d for d in data)
    assert all('credential-must-not-be-archived' not in d for d in data)
    assert all(p.stat().st_mode&0o777==0o600 for p in rows)
    assert (await tokens.report(rid))['charged']==35


async def test_oversized_wire_request_is_never_dispatched():
    runtime=resources();called=[]
    async def handler(request):called.append(request);return httpx.Response(200,json={})
    runtime.http.transports['agent-model']=httpx.MockTransport(handler)
    with pytest.raises(tokens.InputTooLarge):
        await runtime.http.request('agent-model','POST','http://local.test/v1/chat/completions',deadline=OperationDeadline.after(5),
            json={'model':'local','messages':[{'role':'user','content':'x'*40000}]})
    assert not called


async def test_externalized_checkpoint_has_real_receipt_and_detects_corruption():
    from agent_service.checkpoints import ProtectedSaver
    from agent_service.runtime import reserve_model,settle_model,safe_to_resume
    from agent_service.storage.receipts import marker,MARKER_CHANNEL
    from langgraph.checkpoint.base import empty_checkpoint
    rid,token=await claimed();saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    identity=await reserve_model(rid,token,'react','local')
    await settle_model(rid,token,identity,'succeeded')
    checkpoint=empty_checkpoint();checkpoint['v']=4;checkpoint['channel_values']={'big':{'text':'x'*20000,MARKER_CHANNEL:[marker('model',identity)]}}
    checkpoint['channel_versions']={'big':'1'}
    config={'configurable':{'thread_id':str(rid)}}
    await saver.aput(config,checkpoint,{},checkpoint['channel_versions'])
    assert (await saver.aget_tuple(config)).checkpoint['channel_values']['big']['text']=='x'*20000
    async with transaction(readonly=True) as conn:
        assert await safe_to_resume(conn,rid)
        row=await (await conn.execute('SELECT type,octet_length(blob) AS size FROM agent.checkpoint_blobs WHERE thread_id=%s',(str(rid),))).fetchone()
        assert row['type']=='execution-ref-v1' and row['size']<1024
    with owner() as conn:conn.execute("UPDATE agent.execution_bodies SET body='bad'::bytea WHERE run_id=%s",(rid,))
    async with transaction(readonly=True) as conn:assert not await safe_to_resume(conn,rid)


async def test_gc_refuses_live_run_and_preserves_permanent_pins():
    from agent_service.storage.bodies import put_json,pin
    rid,token=await claimed();keep=await put_json(rid,token,{'published':'exact source'});discard=await put_json(rid,token,{'temporary':'state'})
    async with transaction() as conn:await pin(conn,rid,keep,'result:permanent')
    with owner() as conn:
        conn.execute('SET ROLE lifecycle_maintenance')
        assert not conn.execute('SELECT execution.gc_run(%s) AS ok',(rid,)).fetchone()['ok']
        with pytest.raises(psycopg.errors.InsufficientPrivilege):conn.execute('DELETE FROM agent.execution_bodies')
        conn.execute('RESET ROLE')
        conn.execute("UPDATE execution.tasks SET status='completed',finished_at=clock_timestamp()-interval '31 days',lease_token=NULL,leased_until=NULL WHERE id=%s",(rid,))
        conn.execute('SET ROLE lifecycle_maintenance')
        assert conn.execute('SELECT execution.gc_run(%s) AS ok',(rid,)).fetchone()['ok']
        conn.execute('RESET ROLE')
        rows=conn.execute('SELECT sha256 FROM agent.execution_bodies WHERE run_id=%s',(rid,)).fetchall()
        assert [r['sha256'] for r in rows]==[keep['_execution_body']['sha256']]


async def test_orphan_quarantine_and_delayed_delete_preserve_referenced_file(monkeypatch,tmp_path):
    from scripts.lifecycle import collect
    monkeypatch.setenv('RAG_FILES_DIR',str(tmp_path))
    orphan=tmp_path/'upload-orphan';orphan.write_bytes(b'orphan')
    identity=uuid4()
    with owner() as conn:
        conn.execute("INSERT INTO execution.artifacts(namespace,id,path,state,writer_id,writer_until,updated_at) VALUES ('rag',%s,%s,'unknown',%s,clock_timestamp()-interval '2 days',clock_timestamp()-interval '2 days')",(identity,str(orphan),uuid4()))
        conn.execute('SET ROLE lifecycle_maintenance')
        collect(conn)
        assert not orphan.exists() and (tmp_path/'.quarantine'/str(identity)/orphan.name).exists()
        collect(conn)
        assert (tmp_path/'.quarantine'/str(identity)).exists()
        conn.execute('RESET ROLE')
        conn.execute("UPDATE execution.artifacts SET quarantined_at=clock_timestamp()-interval '2 days' WHERE id=%s",(identity,))
        conn.execute('SET ROLE lifecycle_maintenance');collect(conn)
        assert not (tmp_path/'.quarantine'/str(identity)).exists()


async def test_retention_hold_prevents_terminal_gc():
    rid,token=await claimed()
    with owner() as conn:
        conn.execute("UPDATE execution.tasks SET status='failed',finished_at=clock_timestamp()-interval '31 days',lease_token=NULL,leased_until=NULL WHERE id=%s",(rid,))
        conn.execute('SET ROLE lifecycle_maintenance')
        conn.execute("INSERT INTO execution.retention_holds VALUES('agent',%s,'research audit')",(str(rid),))
        assert not conn.execute('SELECT execution.gc_run(%s) AS ok',(rid,)).fetchone()['ok']


async def test_exact_required_sources_cannot_be_silently_truncated():
    from agent_service.context_budget import evidence_view
    from agent_service.tooling import RunExecutionState
    context=RunExecutionState(uuid4(),uuid4(),evidence={'large':{'content':'x'*50000}})
    with pytest.raises(tokens.InputTooLarge):await evidence_view(context,{'large'})
    assert await evidence_view(context)=={}


async def test_compaction_once_per_step_requires_real_checkpoint(monkeypatch):
    from langchain_core.messages import HumanMessage,AIMessage
    from langchain.agents.middleware.types import ModelRequest
    from types import SimpleNamespace
    from tests.native_async import native_model,native_runnable
    from agent_service import execution
    from agent_service.tooling import RunExecutionState
    from agent_service.quality import QualityMiddleware
    from agent_service.checkpoints import ProtectedSaver
    from langgraph.checkpoint.base import empty_checkpoint
    @native_model
    class Compressor:
        def with_structured_output(self,schema):
            return native_runnable(lambda messages:schema.model_validate({'facts':[],'assumptions':[],
                'conditions':['retain uncertainty'],'unresolved':[],'outputs':[]}))
    monkeypatch.setattr(execution,'model',lambda:Compressor())
    rid,token=await claimed()
    context=RunExecutionState(rid,token,original_task='test task',goal_contract={'requirements':[{
        'id':'r1','description':'test task','completion_condition':'deliver','conditions':['retain uncertainty']}]})
    state={'messages':[HumanMessage(content='x'*5000) for _ in range(8)],'goal_contract':context.goal_contract}
    runtime=SimpleNamespace(context=context)
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        update=await QualityMiddleware().abefore_model(state,runtime)
        assert update['context_compaction']['conditions']==['retain uncertainty']
        assert not update.get('acceptance_blocked')
        state.update(update)
        request=ModelRequest(model=None,messages=state['messages'],state=state,runtime=runtime,tools=[])
        async def response(req):return SimpleNamespace(result=[AIMessage(content='done')])
        from agent_service.model_calls import budgeted_model
        with pytest.raises(ValueError,match='checkpoint receipt'):
            await budgeted_model.awrap_model_call(request,response)
        cp=empty_checkpoint();cp['v']=4;cp['channel_values']=update;cp['channel_versions']={k:'1' for k in update}
        saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
        await saver.aput({'configurable':{'thread_id':str(rid)}},cp,{},cp['channel_versions'])
        await budgeted_model.awrap_model_call(request,response)
        state['messages'].append(HumanMessage(content='y'*40000))
        rejected=await QualityMiddleware().abefore_model(state,runtime)
        assert rejected['acceptance_blocked'] and rejected['acceptance_reason']=='CONTEXT_LIMIT_EXCEEDED'
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM agent.model_calls WHERE run_id=%s AND purpose='context_compaction'",(rid,))).fetchone())['n']==1


async def test_grouped_final_verification_is_bound_to_every_actual_part(monkeypatch):
    from tests.test_agent_runtime import ScriptedModel
    from agent_service import execution
    from agent_service.tooling import RunExecutionState
    from agent_service.schemas import FinalEvaluation
    from agent_service.quality import evaluate
    from agent_service.runtime import reserve_model,settle_model
    from agent_service.storage.acceptance import check_call
    from agent_service.acceptance import digest
    class GroupJudge(ScriptedModel):
        def quality_response(self,name,data,value):
            if name=='FinalEvaluation':
                if 'claim_reports' in data:
                    verdicts={r['claim_id']:r for r in data['claim_reports']}
                    for claim in value['claims']:claim['status']=verdicts[claim['claim_id']]['status']
                else:
                    for claim in value['claims']:
                        for source in claim['sources']:
                            source['quote']=source['quote'][:50];source['end']=len(source['quote'])
            return value
    monkeypatch.setattr(execution,'model',lambda:GroupJudge(responses=[]))
    rid,token=await claimed()
    generated=await reserve_model(rid,token,'final','local',final=True)
    await settle_model(rid,token,generated,'succeeded')
    evidence={f'ev-{i}':{'evidence_id':f'ev-{i}','title':str(i),'content':'source fact '+str(i)+' '+('x'*6000),
        'source_locator':{'kind':'txt','line_start':1,'line_end':1}} for i in range(6)}
    context=RunExecutionState(rid,token,original_task='test task',evidence=evidence,
        goal_contract={'requirements':[{'id':'r1','description':'all facts','completion_condition':'deliver all','conditions':[]}]})
    candidate={'answer':'candidate','claims':[{'claim_id':f'c{i}','text':f'fact {i}','support':'evidence','evidence_ids':[f'ev-{i}'],'reason':None} for i in range(6)],
        'blocks':[{'id':f'b{i}','kind':'claim','claim_ids':[f'c{i}'],'requirement_ids':['r1'],'text':''} for i in range(6)],'notices':[]}
    inputs={'task':'test task','contract':context.goal_contract,'evidence':evidence,'candidate':candidate}
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        result,cid,marks,error=await evaluate(FinalEvaluation,{**inputs,'summaries':[]},context,'final_acceptance',final=True,bound_inputs=inputs)
        assert error is None and all(c['status']=='supported' for c in result['claims'])
        assert len(marks)>1
        async with transaction() as conn:await check_call(conn,rid,cid,'FinalEvaluation',result,input_hash=digest(inputs))
    with owner() as conn:conn.execute("UPDATE agent.verification_batches SET value=jsonb_set(value,'{conditions_preserved}','false') WHERE call_id=%s",(cid,))
    async with transaction() as conn:
        with pytest.raises(ValueError,match='identity mismatch'):await check_call(conn,rid,cid,'FinalEvaluation',result,input_hash=digest(inputs))


async def test_unknown_reservation_ack_never_sends_and_retains_capacity(monkeypatch):
    runtime=resources();sent=[]
    async def handler(request):sent.append(request);return httpx.Response(200,json={})
    runtime.http.transports['agent-model']=httpx.MockTransport(handler)
    original=tokens.reserve
    async def lost(*args,**kwargs):
        await original(*args,**kwargs)
        raise PersistenceOutcomeUnknown('Reservation committed but acknowledgement lost')
    monkeypatch.setattr(tokens,'reserve',lost)
    rid,token=await claimed()
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        with pytest.raises(PersistenceOutcomeUnknown):
            await runtime.http.request('agent-model','POST','http://local.test/v1/chat/completions',deadline=OperationDeadline.after(5),json={'model':'local','messages':[]})
    assert not sent and (await tokens.report(rid))['unconfirmed_tokens']>0


async def test_sensitive_fields_and_embedded_json_are_explicitly_redacted():
    from execution.archive import encode
    import base64
    value=json.loads(encode({'api_key':'custom-secret','text':'business condition',
        'arguments':json.dumps({'password':'nested-password','query':'keep'}),
        'body_base64':base64.b64encode(b'\xffknown-secret').decode()},['known-secret']))
    assert value['redactions']==3 and value['data']['text']=='business condition'
    assert 'custom-secret' not in str(value) and 'nested-password' not in str(value)
    assert base64.b64decode(value['data']['body_base64'])==b'\xff[redacted]'


async def test_frozen_tokenizer_rejects_changed_files_before_loading(monkeypatch,tmp_path):
    import threading
    (tmp_path/'tokenizer.json').write_text('{}')
    monkeypatch.setenv('TOKEN_MODEL_POLICIES',json.dumps({'local':{'tokenizer':str(tmp_path)}}))
    selected=tokens.ModelTokenPolicy(**tokens.configuration()['models']['local'])
    (tmp_path/'tokenizer.json').write_text('{"changed":true}')
    with pytest.raises(ValueError,match='changed after policy capture'):
        tokens._count({'messages':[]},selected,{},threading.Lock())
    wire=await tokens.envelope('agent-model',{'model':'other','messages':[],
        'max_tokens':20,'max_completion_tokens':40})
    assert wire.output_tokens==20 and 'max_tokens' not in wire.body


async def test_checkpoint_storage_warning_does_not_break_committed_receipt(monkeypatch,caplog):
    from agent_service.checkpoints import ProtectedSaver
    from langgraph.checkpoint.base import empty_checkpoint
    rid,token=await claimed();saver=ProtectedSaver(TaskRef(rid,TaskKind.AGENT),token)
    monkeypatch.setattr(saver,'STORAGE_WARNING_BYTES',1)
    cp=empty_checkpoint();cp['v']=4;cp['channel_values']={'value':'small'};cp['channel_versions']={'value':'1'}
    config={'configurable':{'thread_id':str(rid)}}
    await saver.aput(config,cp,{},cp['channel_versions'])
    assert (await saver.aget_tuple(config)).checkpoint['channel_values']['value']=='small'
    assert 'checkpoint storage exceeds warning threshold' in caplog.text


async def test_cross_role_allocation_usage_is_not_replayed_or_released_after_dispatch():
    from execution import delegation
    from execution.persistence import using
    from tests.native_async import runtimes
    from contracts.errors import ApiError
    rid,token=await claimed();tool_id=str(uuid4());request={'query':'exact need'}
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        ticket,handle=await delegation.issue(rid,tool_id,request)
        assert (await tokens.report(rid))['charged']==98304
        await tokens.sent(handle)
    with using(runtimes.get()['rag']):
        allocation=delegation.decode(ticket,str(rid),tool_id,request)
        assert await delegation.start(handle[1],str(rid),tool_id,request,allocation) is None
        with pytest.raises(ApiError):await delegation.start(handle[1],str(rid),tool_id,request,allocation)
        usage={'charged':150,'actual_input_tokens':100,'actual_output_tokens':20,'unconfirmed_tokens':30,'blocked':False}
        await delegation.finish(handle[1],{'usage':usage},status='failed')
        assert (await delegation.status(handle[1],str(rid),tool_id))['status']=='failed'
        with pytest.raises(ApiError):await delegation.start(handle[1],str(rid),tool_id,request,allocation)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with transaction(readonly=True) as conn:await conn.execute('SELECT * FROM agent.agent_runs')
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        await delegation.reconcile(handle,usage)
        await delegation.reconcile(handle,usage)
        with pytest.raises(ValueError):await tokens.settle(handle,not_sent=True)
    report=await tokens.report(rid)
    assert report['charged']==150 and report['unconfirmed_tokens']==30


async def test_expired_child_allocation_and_overrun_prevent_new_dispatch():
    from execution import delegation
    from execution.persistence import using
    from tests.native_async import runtimes
    from contracts.errors import ApiError
    import time
    with using(runtimes.get()['rag']):
        with pytest.raises(ApiError):
            await delegation.start(uuid4(),'run','tool',{}, {'expires_at':time.time()-1})
        key=tokens.budget_context.set({'id':'expired','kind':'search','expires_at':time.time()-1})
        try:
            value=await tokens.envelope('embedding',{'model':'local','input':['q']})
            with pytest.raises(tokens.TokenBudgetExceeded):await tokens.reserve(uuid4(),value)
        finally:tokens.budget_context.reset(key)
    rid,token=await claimed()
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        prepared=await tokens.envelope('agent-model',{'model':'local','messages':[]})
        first=await tokens.reserve(uuid4(),prepared);second=await tokens.reserve(uuid4(),prepared)
        await tokens.sent(first)
        await tokens.settle(first,tokens.UsageRecord(prepared.input_tokens+1,0,'provider'))
        with pytest.raises(tokens.TokenBudgetExceeded):await tokens.sent(second)
        await tokens.settle(second,not_sent=True)
    assert (await tokens.report(rid))['blocked']


async def test_active_upload_file_lock_and_real_reference_block_gc(monkeypatch,tmp_path):
    import fcntl
    from scripts.lifecycle import collect
    monkeypatch.setenv('RAG_FILES_DIR',str(tmp_path))
    path=tmp_path/'upload-still-writing';path.write_bytes(b'data');identity=uuid4()
    with owner() as conn:
        conn.execute("INSERT INTO execution.artifacts(namespace,id,path,state,writer_id,writer_until,updated_at) VALUES ('rag',%s,%s,'writing',%s,clock_timestamp()-interval '2 days',clock_timestamp()-interval '2 days')",(identity,str(path),uuid4()))
        with path.with_name(path.name+'.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            conn.execute('SET ROLE lifecycle_maintenance');collect(conn)
            assert path.exists()
            conn.execute('RESET ROLE')
        # A durable version reference also protects a quiet, expired registry.
        document_id,version_id=uuid4(),uuid4()
        conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'protected upload')",(document_id,))
        conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,1,%s,%s,'text/plain','active')",(version_id,document_id,b'x'*32,str(path)))
        conn.execute('SET ROLE lifecycle_maintenance');collect(conn)
        assert path.exists()


async def test_archive_gc_recovers_rename_and_metadata_crashes(monkeypatch,tmp_path):
    from scripts.lifecycle import collect
    monkeypatch.setenv('CALL_ARCHIVE_DIR',str(tmp_path));identity=uuid4()
    incoming=tmp_path/f'agent-{identity}-input.json.gz'
    outgoing=tmp_path/f'agent-{identity}-output.json.gz'
    temporary=tmp_path/(outgoing.name+'.interrupted.tmp')
    for path in (incoming,outgoing,temporary):path.write_bytes(b'old archive')
    os.utime(temporary,(1,1))
    with owner() as conn:
        conn.execute("INSERT INTO execution.call_archives(namespace,id,service,input_path,input_sha256,expires_at,status) VALUES ('agent',%s,'agent-model',%s,'digest',clock_timestamp()-interval '1 day','gc_marked')",(identity,str(incoming)))
        conn.execute('SET ROLE lifecycle_maintenance');collect(conn);collect(conn)
        assert not any(p.exists() for p in (incoming,outgoing,temporary))
        assert not conn.execute('SELECT 1 FROM execution.call_archives WHERE id=%s',(identity,)).fetchone()


async def test_search_rejected_before_send_releases_parent_allocation(monkeypatch):
    from agent_service.rag_client import RagClient,RagError
    from contracts.v1 import EvidenceSearchRequest,ErrorCode
    from execution.flow import DownstreamLimited
    calls=[]
    async def limited(*args,**kwargs):calls.append(args);raise DownstreamLimited()
    monkeypatch.setattr(resources().http,'request',limited)
    rid,token=await claimed()
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        with pytest.raises(RagError) as failure:
            await RagClient('http://local.test',os.environ['RAG_SERVICE_TOKEN']).search(rid,str(uuid4()),EvidenceSearchRequest(query='need'))
    assert failure.value.code==ErrorCode.RAG_RATE_LIMITED and len(calls)==1
    assert (await tokens.report(rid))['charged']==0


async def test_malformed_search_reply_queries_usage_without_repeating_search():
    from agent_service.rag_client import RagClient,RagError
    from contracts.v1 import EvidenceSearchRequest
    calls=[]
    async def handler(request):
        calls.append(request.method)
        if request.method=='POST':return httpx.Response(200,json={'unexpected':'invalid response'})
        return httpx.Response(200,json={'status':'failed','result':{'usage':{
            'actual_input_tokens':20,'actual_output_tokens':0,'charged':20,'unconfirmed_tokens':0,'blocked':False}}})
    resources().http.transports['rag']=httpx.MockTransport(handler)
    rid,token=await claimed()
    with attempt(TaskRef(rid,TaskKind.AGENT),token):
        with pytest.raises(RagError):
            await RagClient('http://local.test',os.environ['RAG_SERVICE_TOKEN']).search(rid,str(uuid4()),EvidenceSearchRequest(query='need'))
    assert calls==['POST','GET'] and (await tokens.report(rid))['charged']==20
