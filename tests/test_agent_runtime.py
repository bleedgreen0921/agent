from tests.acceptance_fixtures import fixture_publish_result
from tests.native_async import native_model
from tests.native_async import native, native_runnable
import hashlib
import json
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest
import httpx
import openai
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain.tools import ToolRuntime, tool
from agent_service import execution
from agent_service.rag_client import RagError
from agent_service.tools import rag as rag_tools
from agent_service.tooling import FatalToolError, RecoverableToolError, ToolDeclaration, ToolExecutionContext, ToolProvider
from agent_service.app import app
from agent_service.runs import cancel_run, get_run
from agent_service.runtime import BudgetExhausted, claim_run, publish_result, reserve_model, reserve_tool, revoke_and_finish, safe_to_resume, settle_model, settle_tool
from contracts.v1 import ErrorCode, Evidence, EvidenceSearchResponse, RunResponse, SourceLocator
from db.connection import connect
from tests.helpers import insert_run, quiesce_runs
from execution.contracts import ToolOutcome, TaskRef, TaskKind
from execution.inputs import experiment_snapshot
pytestmark = pytest.mark.skipif(not os.environ.get('AGENT_DATABASE_URL'), reason='isolated PostgreSQL not configured')

@native_model
class ScriptedModel(FakeMessagesListChatModel):

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):

        def value(_messages):
            if schema is execution.Plan:
                return execution.Plan(steps=[execution.PlanStep(goal='Answer the task', completion_condition='A concise answer is available')])
            return execution.FinalDraft(answer='Synthetic answer', claims=[execution.DraftClaim(text='Synthetic answer', support='unverified', evidence_ids=[], reason='No knowledge-base evidence was requested')])
        return native_runnable(value)

class EvidenceModel(ScriptedModel):

    def with_structured_output(self, schema, **kwargs):
        if schema is not execution.FinalDraft:
            return super().with_structured_output(schema, **kwargs)
        return native_runnable(lambda _messages: execution.FinalDraft(answer='Retention is seven years.', claims=[execution.DraftClaim(text='Seven years', support='evidence', evidence_ids=['ev-tool'])]))

class InvalidPlanModel(ScriptedModel):

    def with_structured_output(self, schema, **kwargs):
        if schema is execution.Plan:

            def invalid(_messages):
                raise ValueError('invalid structured plan')
            return native_runnable(invalid)
        return super().with_structured_output(schema, **kwargs)

class PhaseTimeoutModel(ScriptedModel):
    phase: str

    @staticmethod
    def timeout():
        raise openai.APITimeoutError(request=httpx.Request('POST', 'http://model.test/v1/chat/completions'))

    def _generate(self, *args, **kwargs):
        if self.phase == 'react':
            self.timeout()
        return super()._generate(*args, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        if schema is execution.Plan and self.phase == 'planner' or (schema is execution.FinalDraft and self.phase == 'final'):
            return native_runnable(lambda _messages: self.timeout())
        return super().with_structured_output(schema, **kwargs)
seen_tool_context: list[tuple[UUID | None, UUID]] = []

@tool(response_format='content_and_artifact')
async def echo_value(value: str, runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Return a value for tool-provider integration testing."""
    seen_tool_context.append(runtime.context.run_id)
    content = 'echo:' + value
    return (content, ToolOutcome(content).to_artifact())

@tool(response_format='content_and_artifact')
async def recoverable_value(runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Raise a recoverable provider error for integration testing."""
    raise RecoverableToolError('TEMPORARY_TOOL_ERROR', 'The tool is temporarily unavailable')

@tool(response_format='content_and_artifact')
async def broken_value(runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Raise an unexpected provider error for integration testing."""
    raise RuntimeError('provider implementation failed')

def extra_tools():
    return ToolProvider('test_extra', '1', tuple((ToolDeclaration(item, '1', 'read_only', False) for item in (echo_value, recoverable_value, broken_value))), contract_version=1)

def configure_extra_tools(monkeypatch):
    provider = types.ModuleType('test_extra_tool_provider')
    provider.tools = extra_tools
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    monkeypatch.setenv('AGENT_TOOL_PROVIDERS', provider.__name__ + ':tools')

def checkpoint_tool_messages(run_id: UUID) -> list[ToolMessage]:
    from agent_service.checkpoints import with_agent_search_path
    from langgraph.checkpoint.postgres import PostgresSaver
    dsn = with_agent_search_path(os.environ['AGENT_DATABASE_URL'])
    with PostgresSaver.from_conn_string(dsn) as saver:
        snapshot = execution.react_agent(saver).get_state({'configurable': {'thread_id': str(run_id)}})
    return [message for message in snapshot.values['messages'] if isinstance(message, ToolMessage)]

class FakeRagClient:

    class Client:

        def close(self):
            pass

    def __init__(self, *_args, **_kwargs):
        self.client = self.Client()

    async def search(self, _run_id, _tool_call_id, _request):
        evidence = Evidence(evidence_id='ev-tool', document_id='doc-tool', document_version_id='ver-tool', title='Retention policy', content='Records are kept for seven years.', source_locator=SourceLocator(kind='txt', line_start=3, line_end=3), rank=1)
        return EvidenceSearchResponse(request_id='rag-1', retrieval_id='ret-1', status='ok', evidences=[evidence], degradations=[])

class UnauthorizedRagClient(FakeRagClient):

    async def search(self, _run_id, _tool_call_id, _request):
        raise RagError(401, ErrorCode.UNAUTHENTICATED)

class UnavailableRagClient(FakeRagClient):

    async def search(self, _run_id, _tool_call_id, _request):
        raise RagError(503, ErrorCode.RAG_UNAVAILABLE)

class TimedOutRagClient(FakeRagClient):

    async def search(self, _run_id, _tool_call_id, _request):
        raise RagError(503, ErrorCode.RAG_TIMEOUT)

    async def read(self, _run_id, _tool_call_id, _evidence_id):
        raise RagError(503, ErrorCode.RAG_TIMEOUT)

class PartialPlanModel(ScriptedModel):
    final_prompt: dict | None = None

    def __init__(self):
        super().__init__(responses=[AIMessage(content='unused')])

    def with_structured_output(self, schema, **kwargs):
        if schema is execution.Plan:
            return native_runnable(lambda _messages: execution.Plan(steps=[execution.PlanStep(goal='Collect policy A', completion_condition='Policy A is summarized'), execution.PlanStep(goal='Compare policy B', completion_condition='The policies are compared')]))

        def final(messages):
            raw = messages[-1].content if hasattr(messages[-1], 'content') else messages[-1][1]
            self.final_prompt = json.loads(raw)
            return execution.FinalDraft(answer='Partial answer from policy A.', claims=[execution.DraftClaim(text='Policy A was collected', support='evidence', evidence_ids=['ev-prior'])])
        return native_runnable(final)

class PartialStepAgent:

    def __init__(self):
        self.prompts = []

    async def aget_state(self, _config):
        return types.SimpleNamespace(values={})

    async def ainvoke(self, payload, *, config, context, durability=None):
        prompt = json.loads(payload['messages'][0]['content'])
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            context.evidence['ev-prior'] = {'evidence_id': 'ev-prior', 'document_id': 'doc-prior', 'document_version_id': 'ver-prior', 'title': 'Policy A', 'content': 'Policy A retains records for seven years.', 'source_locator': {'kind': 'txt', 'line_start': 1, 'line_end': 1}}
            context.notices.append({'code': 'STEP_ONE_COMPLETE', 'message': 'Policy A was collected.'})
            from agent_service.quality import evaluate
            from agent_service.schemas import StepEvaluation
            from langgraph.checkpoint.base import empty_checkpoint
            evaluation,cid,marks,error=await evaluate(StepEvaluation,{'original_task':context.original_task,'observations':[{'role':'ai','content':'Policy A summary'}]},context,'step_acceptance',bound_inputs=dict(task=context.original_task,contract=context.goal_contract,evidence=context.evidence,step_id=str(context.step_id),completion_condition=context.completion_condition))
            checkpoint=empty_checkpoint();checkpoint['channel_values']={'step_evaluation':evaluation,'_execution_calls':marks}
            checkpoint['channel_versions']={'step_evaluation':'1','_execution_calls':'1'}
            await self.saver.aput(config,checkpoint,{},checkpoint['channel_versions'])
            return {'messages':[AIMessage(content='Policy A summary')],'step_evaluation':evaluation,'step_evaluation_call_id':cid,'execution_summary':evaluation['summary']}
        raise BudgetExhausted('tool')

def test_run_api_idempotency_cancel_and_trace():
    headers = {'Idempotency-Key': 'same-request'}
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        first = client.post('/v1/runs', headers=headers, json={'task': 'check policy'})
        assert first.status_code == 202
        replay = client.post('/v1/runs', headers=headers, json={'task': 'check policy'})
        assert replay.status_code == 200 and replay.json()['run_id'] == first.json()['run_id']
        assert client.post('/v1/runs', headers=headers, json={'task': 'different'}).status_code == 409
        run_id = first.json()['run_id']
        assert client.get('/v1/runs/' + run_id).status_code == 200
        assert client.post('/v1/runs/' + run_id + '/cancel').json()['status'] == 'cancelled'
        assert client.get('/v1/runs/' + run_id + '/trace').status_code == 200
        assert client.get('/v1/runs/' + run_id + '/timeline').json()['manifest'] is None
        assert client.get('/v1/runs/' + str(uuid4()) + '/timeline').status_code == 404
        assert client.get('/v1/admin/runs').status_code == 404

async def test_claim_budgets_atomic_result_and_historical_snapshot(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '1')
    quiesce_runs()
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    assert claimed and claimed['run_id'] == run_id
    token = claimed['lease_token']
    last_model = None
    for index in range(14):
        last_model = await native(reserve_model, run_id, token, 'react', 'mock-model', input_summary={'message_count': index + 1})
        assert await native(settle_model, run_id, token, last_model, 'succeeded', output_summary={'message_count': 1})
    with pytest.raises(BudgetExhausted):
        await native(reserve_model, run_id, token, 'react', 'mock-model')
    final_call = await native(reserve_model, run_id, token, 'final', 'mock-model', final=True)
    assert await native(settle_model, run_id, token, final_call, 'succeeded')
    tool_call = await native(reserve_tool, run_id, token, 'rag.search', {'query_sha256': 'safe'}, triggering_model_call_id=last_model)
    assert await native(settle_tool, run_id, token, tool_call, 'succeeded', evidence_ids=['ev-1'])
    evidence = {'ev-1': {'evidence_id': 'ev-1', 'document_id': 'doc-1', 'document_version_id': 'ver-1', 'title': 'Policy', 'content': 'Retention is seven years.', 'source_locator': {'kind': 'txt', 'line_start': 4, 'line_end': 4}}}
    draft = {'answer': 'Retention is seven years.', 'claims': [{'text': 'Seven years', 'support': 'evidence', 'evidence_ids': ['ev-1'], 'reason': None}], 'citations': [evidence['ev-1']], 'notices': []}
    assert await native(fixture_publish_result, run_id, token, draft, evidence, False, 'model_finished')
    result = await native(get_run, run_id)
    assert result['status'] == 'completed'
    assert result['result']['citations'][0]['content'] == 'Retention is seven years.'
    with connect('AGENT_DATABASE_URL') as conn:
        row = conn.execute('SELECT model_calls_used,tool_calls_used FROM agent.agent_runs WHERE id=%s', (run_id,)).fetchone()
        linked = conn.execute('SELECT triggering_model_call_id,argument_summary FROM agent.tool_calls WHERE id=%s', (tool_call,)).fetchone()
    assert row == {'model_calls_used': 15, 'tool_calls_used': 1}
    assert linked['triggering_model_call_id'] == last_model
    assert linked['argument_summary'] == {'query_sha256': 'safe'}

async def test_queue_timeout_unknown_interruption_and_running_cancel(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '1')
    quiesce_runs()
    expired = insert_run(queue_delta=timedelta(seconds=-1), created_delta=timedelta(days=-3))
    interrupted = insert_run(status='running', created_delta=timedelta(days=-2), lease_delta=timedelta(seconds=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status) VALUES (%s,%s,'react','mock','started')", (uuid4(), interrupted))
    assert await native(claim_run) is None
    with connect('AGENT_DATABASE_URL') as conn:
        states = {row['id']: (row['status'], row['error_code']) for row in conn.execute('SELECT id,status,error_code FROM agent.run_state WHERE id IN (%s,%s)', (expired, interrupted)).fetchall()}
    assert states[expired] == ('failed', 'QUEUE_TIMEOUT')
    assert states[interrupted] == ('failed', 'INTERRUPTED_UNKNOWN')
    cancellable = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    assert claimed and claimed['run_id'] == cancellable
    payload, status = await native(cancel_run, cancellable)
    assert status == 202 and payload['status'] == 'cancelling'
    await native(revoke_and_finish, cancellable, claimed['lease_token'], 'cancel')
    assert (await native(get_run, cancellable))['status'] == 'cancelled'

async def test_resume_requires_every_settled_call_to_be_checkpointed():
    quiesce_runs()
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    token = claimed['lease_token']
    call_id = await native(reserve_model, run_id, token, 'react', 'mock-model')
    assert await native(settle_model, run_id, token, call_id, 'succeeded')
    from agent_service.checkpoints import ProtectedSaver
    from langgraph.checkpoint.base import empty_checkpoint
    from agent_service.storage.receipts import MARKER,marker
    saver=ProtectedSaver(TaskRef(run_id,TaskKind.AGENT),token)
    cp=empty_checkpoint();cp['channel_values']={'messages':[AIMessage(content='saved',additional_kwargs={MARKER:marker('model',call_id)})]};cp['channel_versions']={'messages':'1'}
    with connect('AGENT_DATABASE_URL') as conn:assert not await native(safe_to_resume,conn,run_id)
    await saver.aput({'configurable':{'thread_id':str(run_id),'checkpoint_ns':''}},cp,{}, {'messages':'1'})
    with connect('AGENT_DATABASE_URL') as conn:assert await native(safe_to_resume,conn,run_id)
    final_call=await native(reserve_model,run_id,token,'final','mock-model',final=True)
    await native(settle_model,run_id,token,final_call,'succeeded')
    with connect('AGENT_DATABASE_URL') as conn:assert not await native(safe_to_resume,conn,run_id)

async def test_expired_cancelling_run_is_finalized(monkeypatch):
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '1')
    quiesce_runs()
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(reserve_model, run_id, claimed['lease_token'], 'react', 'mock-model')
    payload, status = await native(cancel_run, run_id)
    assert status == 202 and payload['status'] == 'cancelling'
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE execution.tasks SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    assert await native(claim_run) is None
    with connect('AGENT_DATABASE_URL') as conn:
        run = conn.execute('SELECT status,termination_reason,lease_token FROM agent.run_state WHERE id=%s', (run_id,)).fetchone()
        call = conn.execute('SELECT status,error_code FROM agent.model_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert run == {'status': 'cancelled', 'termination_reason': 'cancelled', 'lease_token': None}
    assert call == {'status': 'interrupted_unknown', 'error_code': 'INTERRUPTED_UNKNOWN'}

@pytest.mark.parametrize(('mode', 'expected_calls', 'expected_steps'), [('react', 5, 0), ('plan_execute', 6, 1)])
async def test_langgraph_dual_mode_execution(monkeypatch, mode, expected_calls, expected_steps):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=[AIMessage(content='Execution summary')]))
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE agent.agent_runs SET mode=%s WHERE id=%s', (mode, run_id))
    claimed = await native(claim_run)
    assert claimed and claimed['run_id'] == run_id
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'completed'
    assert 'Synthetic answer' in result['result']['answer'] and '未核实' in result['result']['answer']
    with connect('AGENT_DATABASE_URL') as conn:
        run = conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (run_id,)).fetchone()
        steps = conn.execute("SELECT count(*) AS n FROM agent.run_steps WHERE run_id=%s AND status='completed'", (run_id,)).fetchone()['n']
    assert run['model_calls_used'] == expected_calls
    assert steps == expected_steps

async def test_react_tool_execution_and_citation_snapshot(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'service-token')
    messages = [AIMessage(content='', tool_calls=[{'name': 'search_evidence', 'args': {'query': 'retention', 'top_k': 5}, 'id': 'tool-1', 'type': 'tool_call'}]), AIMessage(content='Evidence was found.')]
    monkeypatch.setattr(execution, 'model', lambda: EvidenceModel(responses=messages))
    monkeypatch.setattr(rag_tools, 'RagClient', FakeRagClient)
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'completed'
    assert result['result']['citations'][0]['evidence_id'] == 'ev-tool'
    with connect('AGENT_DATABASE_URL') as conn:
        tool_call = conn.execute('SELECT status,retrieval_id,evidence_ids,triggering_model_call_id FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert tool_call['status'] == 'succeeded'
    assert tool_call['retrieval_id'] == 'ret-1'
    assert tool_call['evidence_ids'] == ['ev-tool']
    assert tool_call['triggering_model_call_id'] is not None

async def test_fatal_tool_auth_error_terminates_run(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'service-token')
    messages = [AIMessage(content='', tool_calls=[{'name': 'search_evidence', 'args': {'query': 'retention'}, 'id': 'tool-auth', 'type': 'tool_call'}])]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    monkeypatch.setattr(rag_tools, 'RagClient', UnauthorizedRagClient)
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'AUTH_FAILED'
    with connect('AGENT_DATABASE_URL') as conn:
        call = conn.execute('SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert call == {'status': 'failed', 'error_code': 'UNAUTHENTICATED'}

async def test_rag_unavailable_degrades_without_failing_run(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'service-token')
    messages = [AIMessage(content='', tool_calls=[{'name': 'search_evidence', 'args': {'query': 'retention'}, 'id': 'tool-unavailable', 'type': 'tool_call'}]), AIMessage(content='RAG was unavailable.')]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    monkeypatch.setattr(rag_tools, 'RagClient', UnavailableRagClient)
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'completed'
    assert any((item['code'] == 'RAG_UNAVAILABLE' for item in result['result']['notices']))
    with connect('AGENT_DATABASE_URL') as conn:
        call = conn.execute('SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert call == {'status': 'degraded', 'error_code': 'RAG_UNAVAILABLE'}

@pytest.mark.parametrize(('mode', 'phase', 'expected_calls'), [('react', 'react', 2), ('plan_execute', 'planner', 2), ('react', 'final', 4), ('plan_execute', 'final', 5)])
async def test_model_timeout_fails_run_and_settles_single_call(monkeypatch, mode, phase, expected_calls):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setattr(execution, 'model', lambda: PhaseTimeoutModel(phase=phase, responses=[AIMessage(content='Execution summary')]))
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE agent.agent_runs SET mode=%s WHERE id=%s', (mode, run_id))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = RunResponse.model_validate(await native(get_run, run_id))
    assert result.status == 'failed'
    assert result.error.code == ErrorCode.MODEL_TIMEOUT
    with connect('AGENT_DATABASE_URL') as conn:
        run = conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (run_id,)).fetchone()
        calls = conn.execute('SELECT purpose,status,error_code FROM agent.model_calls WHERE run_id=%s ORDER BY started_at,id', (run_id,)).fetchall()
    assert run['model_calls_used'] == expected_calls
    assert len(calls) == expected_calls
    assert len([call for call in calls if call['error_code'] == 'MODEL_TIMEOUT']) == 1
    assert all((call['status'] != 'started' for call in calls))

@pytest.mark.parametrize('reason', ['cancel', 'timeout'])
async def test_revoked_lease_cannot_reclassify_timeout(monkeypatch, reason):
    quiesce_runs()
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    token = claimed['lease_token']
    call_id = await native(reserve_model, run_id, token, 'react', 'mock-model')
    if reason == 'cancel':
        await native(cancel_run, run_id)
    else:
        with connect('AGENT_DATABASE_URL') as conn:
            conn.execute("UPDATE execution.tasks SET execution_deadline_at=now()-interval '1 second' WHERE id=%s", (run_id,))
    assert not await native(settle_model, run_id, token, call_id, 'failed', error_code='MODEL_TIMEOUT')
    from agent_service.runtime import fail_run
    assert await native(fail_run, run_id, token, 'MODEL_TIMEOUT')
    await native(revoke_and_finish, run_id, token, reason)
    assert not await native(settle_model, run_id, token, call_id, 'failed', error_code='MODEL_TIMEOUT')
    assert not await native(fail_run, run_id, token, 'MODEL_TIMEOUT')
    with connect('AGENT_DATABASE_URL') as conn:
        row = conn.execute('SELECT status,error_code FROM agent.run_state WHERE id=%s', (run_id,)).fetchone()
    assert row == ({'status': 'cancelled', 'error_code': None} if reason == 'cancel' else {'status': 'failed', 'error_code': 'RUN_TIMEOUT'})

@pytest.mark.parametrize(('mode', 'expected_models'), [('react', 6), ('plan_execute', 7)])
async def test_resumed_run_uses_manifest_timeout_snapshot(monkeypatch, mode, expected_models):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'service-token')
    monkeypatch.setenv('AGENT_MODEL_TIMEOUT_SECONDS', '7')
    monkeypatch.setenv('AGENT_RAG_TIMEOUT_SECONDS', '11')
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE agent.agent_runs SET mode=%s WHERE id=%s', (mode, run_id))
    first = await native(claim_run)
    with connect('AGENT_DATABASE_URL') as conn:
        manifest = conn.execute('SELECT manifest FROM agent.run_manifests WHERE run_id=%s', (run_id,)).fetchone()['manifest']
        conn.execute("UPDATE execution.tasks SET leased_until=now()-interval '1 second' WHERE id=%s", (run_id,))
    assert manifest['limits']['model_timeout_seconds'] == 7
    assert manifest['limits']['rag_timeout_seconds'] == 11
    monkeypatch.setenv('AGENT_MODEL_TIMEOUT_SECONDS', '17')
    monkeypatch.setenv('AGENT_RAG_TIMEOUT_SECONDS', '19')
    resumed = await native(claim_run)
    assert resumed and resumed['run_id'] == run_id and (not resumed['first_claim'])
    assert resumed['lease_token'] != first['lease_token']
    seen = []
    rag_timeouts = []

    class CapturingRagClient(FakeRagClient):

        def __init__(self, *args, **kwargs):
            rag_timeouts.append(kwargs['timeout_seconds'])
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(rag_tools, 'RagClient', CapturingRagClient)

    def scripted_model():
        seen.append(execution.current_timeouts())
        return ScriptedModel(responses=[AIMessage(content='', tool_calls=[{'name': 'search_evidence', 'args': {'query': 'snapshot'}, 'id': 'snapshot-tool', 'type': 'tool_call'}]), AIMessage(content='Execution summary')])
    monkeypatch.setattr(execution, 'model', scripted_model)
    await native(execution.execute_run, run_id, resumed['lease_token'])
    assert (await native(get_run, run_id))['status'] == 'completed'
    assert len(seen) == expected_models
    assert all((item.model_seconds == 7 and item.rag_seconds == 11 for item in seen))
    assert rag_timeouts == [11]

@pytest.mark.parametrize(('tool_name', 'expected_status', 'expected_notice'), [('search_evidence', 'degraded', 'RAG_UNAVAILABLE'), ('read_evidence', 'failed', None)])
async def test_rag_timeout_search_and_read_observations(monkeypatch, tool_name, expected_status, expected_notice):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setenv('RAG_BASE_URL', 'http://rag.test')
    monkeypatch.setenv('RAG_SERVICE_TOKEN', 'service-token')
    args = {'query': 'retention'} if tool_name == 'search_evidence' else {'evidence_id': 'ev-missing'}
    messages = [AIMessage(content='', tool_calls=[{'name': tool_name, 'args': args, 'id': 'tool-timeout', 'type': 'tool_call'}]), AIMessage(content='The tool timed out.')]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    monkeypatch.setattr(rag_tools, 'RagClient', TimedOutRagClient)
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'completed'
    assert expected_notice in [item['code'] for item in result['result']['notices']] if expected_notice else not any((item['code'] == 'RAG_UNAVAILABLE' for item in result['result']['notices']))
    with connect('AGENT_DATABASE_URL') as conn:
        run = conn.execute('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s', (run_id,)).fetchone()
        call = conn.execute('SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert run['tool_calls_used'] == 1
    assert call == {'status': expected_status, 'error_code': 'RAG_TIMEOUT'}

async def test_invalid_plan_has_stable_failure_code(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    monkeypatch.setattr(execution, 'model', lambda: InvalidPlanModel(responses=[AIMessage(content='unused')]))
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE agent.agent_runs SET mode='plan_execute' WHERE id=%s", (run_id,))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'INVALID_PLAN'

async def test_plan_budget_partial_preserves_prior_step_state(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    plan_model = PartialPlanModel()
    step_agent = PartialStepAgent()
    monkeypatch.setattr(execution, 'model', lambda: plan_model)
    monkeypatch.setattr(execution, 'react_agent', lambda saver: setattr(step_agent,'saver',saver) or step_agent)
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("UPDATE agent.agent_runs SET mode='plan_execute' WHERE id=%s", (run_id,))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = await native(get_run, run_id)
    assert result['status'] == 'partial'
    assert result['result']['citations'][0]['evidence_id'] == 'ev-prior'
    assert any((item['code'] == 'STEP_ONE_COMPLETE' for item in result['result']['notices']))
    assert plan_model.final_prompt['execution_summaries'][0]['outputs'] == ['Policy A summary']
    assert step_agent.prompts[1]['prior_step_summaries'][0]['outputs'] == ['Policy A summary']
    assert step_agent.prompts[1]['evidence_catalog'][0]['evidence_id'] == 'ev-prior'
    with connect('AGENT_DATABASE_URL') as conn:
        steps = conn.execute('SELECT status,error_code FROM agent.run_steps WHERE run_id=%s ORDER BY ordinal', (run_id,)).fetchall()
    assert steps == [{'status': 'completed', 'error_code': None}, {'status': 'failed', 'error_code': 'QUOTA_EXCEEDED'}]

async def test_global_concurrency_slots_are_atomic(monkeypatch):
    quiesce_runs()
    monkeypatch.setenv('AGENT_MAX_CONCURRENT_RUNS', '2')
    run_ids = {insert_run(created_delta=timedelta(days=-1, seconds=index)) for index in range(4)}
    try:
        claims = [claim for claim in await asyncio.gather(*(native(claim_run) for _ in range(4))) if claim]
        assert len(claims) == 2
        assert len({claim['run_id'] for claim in claims}) == 2
        assert {claim['run_id'] for claim in claims} <= run_ids
        with connect('AGENT_DATABASE_URL') as conn:
            running = conn.execute("SELECT count(*) AS n FROM agent.run_state WHERE id=ANY(%s) AND status='running'", (list(run_ids),)).fetchone()['n']
        assert running == 2
    finally:
        quiesce_runs()

async def test_non_rag_tool_provider_runs_without_execution_changes(monkeypatch):
    quiesce_runs()
    seen_tool_context.clear()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    messages = [AIMessage(content='', tool_calls=[{'name': 'echo_value', 'args': {'value': 'hello'}, 'id': 'extra-1', 'type': 'tool_call'}]), AIMessage(content='The extra tool returned its value.')]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    assert (await native(get_run, run_id))['status'] == 'completed'
    with connect('AGENT_DATABASE_URL') as conn:
        call = conn.execute('SELECT tool_name,status,argument_summary FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert call['tool_name'] == 'echo_value'
    assert call['status'] == 'succeeded'
    assert call['argument_summary']['argument_names'] == ['value']
    assert 'hello' not in json.dumps(call['argument_summary'])
    assert seen_tool_context == [run_id]

@pytest.mark.parametrize('tool_call', [{'name': 'unknown_tool', 'args': {}, 'id': 'unknown-1', 'type': 'tool_call'}, {'name': 'echo_value', 'args': {}, 'id': 'invalid-1', 'type': 'tool_call'}], ids=['unknown-tool', 'invalid-arguments'])
async def test_langgraph_rejected_tool_call_is_traced_as_failed(monkeypatch, tool_call):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    messages = [AIMessage(content='', tool_calls=[tool_call]), AIMessage(content='The tool request was rejected.')]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    assert (await native(get_run, run_id))['status'] == 'partial'
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT count(*) AS n FROM agent.tool_calls WHERE run_id=%s',(run_id,)).fetchone()['n']==0

async def test_recoverable_tool_error_has_consistent_message_and_trace_status(monkeypatch):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    messages = [AIMessage(content='', tool_calls=[{'name': 'recoverable_value', 'args': {}, 'id': 'recoverable-1', 'type': 'tool_call'}]), AIMessage(content='The temporary tool error was handled.')]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    assert (await native(get_run, run_id))['status'] == 'completed'
    with connect('AGENT_DATABASE_URL') as conn:
        call = conn.execute('SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert call == {'status': 'failed', 'error_code': 'TEMPORARY_TOOL_ERROR'}
    tool_messages = checkpoint_tool_messages(run_id)
    assert any((message.tool_call_id == 'recoverable-1' and message.status == 'error' for message in tool_messages))

class ExperimentEvidenceModel(ScriptedModel):

    def with_structured_output(self, schema, **kwargs):
        if schema is not execution.FinalDraft:
            return super().with_structured_output(schema, **kwargs)

        def value(messages):
            payload = json.loads(messages[-1][1])
            evidence = next((item for item in payload['available_evidence'] if item['title'] == 'compare_experiment_metrics'))
            return execution.FinalDraft(answer='合成实验准确率差异为 5.5 个百分点。', claims=[execution.DraftClaim(text='合成实验差异为 5.5 个百分点。', support='evidence', evidence_ids=[evidence['evidence_id']])])
        return native_runnable(value)

@pytest.mark.parametrize('mode', ['react', 'plan_execute'])
async def test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes(monkeypatch, tmp_path, mode):
    import psycopg
    from scripts.synthetic_experiments import generate
    quiesce_runs()
    root = tmp_path / 'experiment-inputs'
    generate(root)
    monkeypatch.setenv('AGENT_TOOL_PROVIDERS', 'agent_service.tools.experiments:tools')
    monkeypatch.setenv('AGENT_EXPERIMENT_ROOT', str(root))
    monkeypatch.setenv('AGENT_MODEL', 'scripted-experiments')
    requests = [('search_experiments', {'filters': {'model.attention.enabled': True}}), ('find_experiment_controls', {'baseline_id': '20261001_090000', 'changed_paths': ['model.attention.enabled']}), ('compare_experiment_metrics', {'left_id': '20261001_090000', 'right_id': '20261001_103000', 'changed_paths': ['model.attention.enabled']})]
    messages = [AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'exp-call-{index}', 'type': 'tool_call'}]) for index, (name, args) in enumerate(requests)]
    messages.append(AIMessage(content='Comparison complete'))
    monkeypatch.setattr(execution, 'model', lambda: ExperimentEvidenceModel(responses=messages))
    run_id = insert_run(created_delta=timedelta(days=-1))
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute('UPDATE agent.agent_runs SET mode=%s WHERE id=%s', (mode, run_id))
    claimed = await native(claim_run)
    assert claimed['run_id'] == run_id
    await native(execution.execute_run, run_id, claimed['lease_token'])
    response = RunResponse.model_validate(await native(get_run, run_id))
    with connect('AGENT_DATABASE_URL') as diagnostics:
        final_check=diagnostics.execute("SELECT payload->>'unavailable' AS unavailable,payload->>'prior_partial' AS prior_partial FROM agent.run_verifications WHERE run_id=%s AND phase='final'",(run_id,)).fetchone()
        purposes=diagnostics.execute('SELECT purpose,status,error_code,input_summary FROM agent.model_calls WHERE run_id=%s ORDER BY started_at',(run_id,)).fetchall()
    assert response.status == 'completed' and response.result is not None, json.dumps({'verification':final_check,'purposes':purposes,'usage':response.token_usage},default=str)
    citation = response.result.citations[0]
    assert citation.source_locator.kind == 'experiment' and citation.document_id is None
    assert set(citation.source_locator.experiment_ids) == {'20261001_090000', '20261001_103000'}
    assert json.loads(citation.content)['data']['aggregates']['overall']['delta_percentage_points'] == 5.5
    assert any((item['code'] == 'EXPERIMENT_SYNTHETIC' for item in response.result.notices))
    with connect('AGENT_DATABASE_URL') as conn:
        assert conn.execute('SELECT count(*) AS n FROM agent.run_experiment_snapshots WHERE run_id=%s', (run_id,)).fetchone()['n'] == 1
        run = conn.execute('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s', (run_id,)).fetchone()
        assert run['tool_calls_used'] == 3
        calls = conn.execute('SELECT status,evidence_ids,triggering_model_call_id FROM agent.tool_calls\n            WHERE run_id=%s ORDER BY started_at,id', (run_id,)).fetchall()
        assert all((row['status'] == 'succeeded' and row['evidence_ids'] and row['triggering_model_call_id'] for row in calls))
    for statement in ("UPDATE agent.run_experiment_snapshots SET snapshot='{}'::jsonb WHERE run_id=%s", 'DELETE FROM agent.run_experiment_snapshots WHERE run_id=%s'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect('AGENT_DATABASE_URL') as conn:
                conn.execute(statement, (run_id,))
    (root / 'experiments/20261001_090000/config.yaml').write_text('changed after publication')
    again = RunResponse.model_validate(await native(get_run, run_id))
    assert again.result.citations == response.result.citations

async def test_experiment_snapshot_concurrency_and_durable_lease_checks(monkeypatch, tmp_path):
    from agent_service.tools import experiments
    from scripts.synthetic_experiments import generate
    quiesce_runs()
    root = tmp_path / 'experiment-inputs'
    generate(root)
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    settings = experiments.Settings(root)
    task = TaskRef(run_id, TaskKind.AGENT)

    async def capture():
        return await native(experiment_snapshot, task, claimed['lease_token'], settings, experiments.VERSION)
    snapshots = await asyncio.gather(capture(),capture())
    assert snapshots[0] == snapshots[1]
    with pytest.raises(PermissionError):
        await native(experiment_snapshot, task, uuid4(), settings, experiments.VERSION)
    await native(cancel_run, run_id)
    with pytest.raises(PermissionError):
        await native(capture)

async def test_unhandled_tool_error_has_distinct_public_run_failure(monkeypatch):
    quiesce_runs()
    configure_extra_tools(monkeypatch)
    monkeypatch.setenv('AGENT_MODEL', 'scripted')
    messages = [AIMessage(content='', tool_calls=[{'name': 'broken_value', 'args': {}, 'id': 'broken-1', 'type': 'tool_call'}])]
    monkeypatch.setattr(execution, 'model', lambda: ScriptedModel(responses=messages))
    run_id = insert_run(created_delta=timedelta(days=-1))
    claimed = await native(claim_run)
    await native(execution.execute_run, run_id, claimed['lease_token'])
    result = RunResponse.model_validate(await native(get_run, run_id))
    assert result.status == 'failed'
    assert result.error.code == ErrorCode.TOOL_CALL_FAILED
    assert result.error.message == 'A tool provider failed'
    with connect('AGENT_DATABASE_URL') as conn:
        call = conn.execute('SELECT status,error_code FROM agent.tool_calls WHERE run_id=%s', (run_id,)).fetchone()
    assert call == {'status': 'failed', 'error_code': 'TOOL_CALL_FAILED'}
