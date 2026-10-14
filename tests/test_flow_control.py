from tests.acceptance_fixtures import fixture_publish_result,prepare_tool
"""Real database competition, isolated tools, and trusted progress timeout tests."""
import asyncio
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
import os
from types import SimpleNamespace
from uuid import uuid4
import threading

import httpx
import openai
import psycopg
import pytest
from psycopg.types.json import Jsonb
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest, ToolRuntime

from agent_service import runtime
from agent_service.storage.runs import create_run, cancel_run
from agent_service.storage.conversations import create_conversation, submit_turn, TurnCreate
from agent_service.tooling import RunExecutionState, budgeted_tool
from agent_service.model_calls import budgeted_model, ToolBatchTooLarge
from contracts.errors import ApiError
from contracts.v1 import RunCreate
from execution import store
from execution.admission import AdmissionStore
from execution.contracts import TaskKind, TaskRef, ToolOutcome
from execution.flow import DownstreamGovernor, DownstreamLimited, retry_seconds
from execution.flow_config import downstream_policy
from execution.infra.types import OperationDeadline, WorkOwner
from execution.persistence import resources, transaction, bind
from execution.progress import ProgressStore
from execution.scope import AttemptContext, attempt
from execution.tool_capacity import tool_slot
from tests.helpers import insert_run
from tests.native_async import native
from db.connection import connect

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


def high_submit(monkeypatch, prefix='AGENT'):
    monkeypatch.setenv(prefix + '_SUBMIT_RATE', '10000')
    monkeypatch.setenv(prefix + '_SUBMIT_BURST', '10000')


async def claim():
    rid = insert_run()
    job = await runtime.claim_run()
    return rid, job['lease_token']


def query(sql, args=()):
    with connect('MIGRATION_DATABASE_URL') as conn:
        cursor = conn.execute(sql, args)
        return cursor.fetchall() if cursor.description else []


async def test_concurrent_task_admission_is_atomic_and_replay_does_not_charge(monkeypatch):
    high_submit(monkeypatch)
    monkeypatch.setenv('AGENT_QUEUE_CAPACITY', '3')
    jobs = await asyncio.gather(*(create_run(RunCreate(task='same'), str(i)) for i in range(8)), return_exceptions=True)
    accepted = [j for j in jobs if not isinstance(j, BaseException)]
    assert len(accepted) == 3
    assert all(j.code == 'QUEUE_CAPACITY_EXCEEDED' for j in jobs if isinstance(j, ApiError))
    before = query("SELECT tokens FROM execution.flow_buckets WHERE scope='agent_run'")[0]['tokens']
    key = next(str(i) for i, j in enumerate(jobs) if not isinstance(j, BaseException))
    replay, existing = await create_run(RunCreate(task='same'), key)
    assert existing and replay['run_id'] in [a[0]['run_id'] for a in accepted]
    assert query("SELECT tokens FROM execution.flow_buckets WHERE scope='agent_run'")[0]['tokens'] == before
    assert query("SELECT count(*) AS n FROM agent.agent_runs")[0]['n'] == 3


async def test_submission_rate_and_unfinished_capacity_have_separate_errors(monkeypatch):
    monkeypatch.setenv('AGENT_SUBMIT_RATE', '.0001')
    monkeypatch.setenv('AGENT_SUBMIT_BURST', '1')
    await create_run(RunCreate(task='one'), 'one')
    with pytest.raises(ApiError) as error: await create_run(RunCreate(task='two'), 'two')
    assert error.value.code == 'SUBMISSION_RATE_LIMITED' and error.value.status == 429
    assert int(error.value.headers['Retry-After']) > 1


async def test_no_worker_grace_then_refusal_and_online_recovery(monkeypatch):
    high_submit(monkeypatch)
    await create_run(RunCreate(task='one'), None)
    query("UPDATE execution.flow_buckets SET unhealthy_since=clock_timestamp()-interval '31 seconds' WHERE scope='agent_run'")
    with pytest.raises(ApiError) as error: await create_run(RunCreate(task='two'), None)
    assert error.value.status == 503
    query('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (uuid4(),))
    payload, replay = await create_run(RunCreate(task='two'), None)
    assert payload['status'] == 'queued' and not replay


async def test_memory_reserved_summary_can_publish_when_capacity_full(monkeypatch):
    high_submit(monkeypatch)
    monkeypatch.setenv('MEMORY_UNFINISHED_CAPACITY', '3')
    conversation = await create_conversation()
    from fastapi import Response
    turn = await submit_turn(__import__('uuid').UUID(conversation['conversation_id']), TurnCreate(task='remember'), Response(), 'turn')
    job = await runtime.claim_run()
    async with transaction() as conn:
        await store.create(conn, TaskRef(uuid4(), TaskKind.MEMORY))
    assert query('SELECT sum(slots) AS n FROM execution.capacity_reservations')[0]['n'] == 1
    draft = {'answer': 'done', 'claims': [], 'citations': [], 'notices': []}
    assert await fixture_publish_result(job['run_id'], job['lease_token'], draft, {}, False, 'model_finished')
    assert query("SELECT count(*) AS n FROM agent.memory_jobs WHERE kind='summary'")[0]['n'] == 1
    assert query('SELECT count(*) AS n FROM execution.capacity_reservations')[0]['n'] == 0


async def test_memory_capacity_rejection_rolls_back_turn_and_run(monkeypatch):
    high_submit(monkeypatch)
    monkeypatch.setenv('MEMORY_UNFINISHED_CAPACITY', '1')
    conversation = await create_conversation()
    from fastapi import Response
    with pytest.raises(ApiError):
        await submit_turn(__import__('uuid').UUID(conversation['conversation_id']), TurnCreate(task='remember'), Response(), 'turn')
    assert query('SELECT count(*) AS n FROM agent.agent_runs')[0]['n'] == 0
    assert query('SELECT count(*) AS n FROM agent.conversation_turns')[0]['n'] == 0
    assert query('SELECT count(*) AS n FROM execution.capacity_reservations')[0]['n'] == 0


async def test_cancel_releases_unused_memory_reservation(monkeypatch):
    high_submit(monkeypatch)
    conversation = await create_conversation()
    from fastapi import Response
    from uuid import UUID
    turn = await submit_turn(UUID(conversation['conversation_id']), TurnCreate(task='remember'), Response(), 'turn')
    await cancel_run(UUID(turn['run_id']))
    assert query('SELECT count(*) AS n FROM execution.capacity_reservations')[0]['n'] == 0


async def test_shared_downstream_bucket_and_policy_conflict(monkeypatch):
    governor = DownstreamGovernor()
    permits = await asyncio.gather(*(governor.acquire('embedding', 'http://synthetic.local/v1', 10) for _ in range(6)))
    assert sum(p is not None for p, delay in permits) == 2
    key, _ = downstream_policy('embedding', 'http://synthetic.local/v1')
    assert downstream_policy('rewrite', 'http://synthetic.local/other')[0] == key
    monkeypatch.setenv('DOWNSTREAM_REQUEST_RATE', '2')
    with pytest.raises(RuntimeError, match='policy differs'):
        await governor.acquire('embedding', 'http://synthetic.local/v1', 10)


@pytest.mark.parametrize('header', ['60', 'Wed, 14 Oct 2026 00:00:00 GMT', None, 'invalid', 'nan', '-1'])
async def test_rate_limit_cooldown_headers_and_single_probe(header):
    governor = DownstreamGovernor()
    permit, _ = await governor.acquire('embedding', 'http://synthetic.local', 10)
    await governor.feedback(permit, status=429, retry_after=header)
    row = query("SELECT * FROM execution.flow_buckets WHERE scope='downstream'")[0]
    assert row['rate_factor'] == .5 and row['unhealthy_since'] is not None
    blocked, delay = await governor.acquire('rewrite', 'http://synthetic.local', 10)
    assert blocked is None and delay > 0
    query("UPDATE execution.flow_buckets SET cooldown_until=clock_timestamp()-interval '1 second',sampled_at=clock_timestamp()-interval '10 seconds'")
    probes = await asyncio.gather(*(governor.acquire('embedding', 'http://synthetic.local', 10) for _ in range(4)))
    assert sum(p is not None for p, _ in probes) == 1
    probe = next(p for p, _ in probes if p)
    await governor.feedback(permit, status=200)
    assert query('SELECT unhealthy_since FROM execution.flow_buckets')[0]['unhealthy_since'] is not None
    await governor.feedback(probe, status=200)
    assert query('SELECT unhealthy_since FROM execution.flow_buckets')[0]['unhealthy_since'] is None


async def test_http_cold_request_never_dispatches_or_keeps_connection_capacity():
    sent = []
    async def server(request):
        sent.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'}, json={'error': {'message': 'slow down', 'type': 'rate_limit_error', 'code': 'rate_limit'}})
    r = resources()
    r.http.transports['embedding'] = httpx.MockTransport(server)
    response = await r.http.request('embedding', 'POST', 'http://synthetic.local/v1/embeddings', deadline=OperationDeadline.after(5))
    assert response.status_code == 429
    with pytest.raises(DownstreamLimited):
        await r.http.request('embedding', 'POST', 'http://synthetic.local/v1/embeddings', deadline=OperationDeadline.after(5))
    assert len(sent) == 1 and r.http.gates['embedding'].active == 0


async def test_sdk_rate_limit_has_no_hidden_retry_and_releases_gate():
    sent = []
    async def server(request):
        sent.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'}, json={'error': {'message': 'slow', 'type': 'rate_limit_error', 'code': 'rate_limit'}})
    r = resources(); r.http.transports['agent-model'] = httpx.MockTransport(server)
    sdk = openai.AsyncOpenAI(api_key='synthetic', base_url='http://synthetic.local/v1', max_retries=0, http_client=r.http.sdk_client('agent-model'))
    with pytest.raises(openai.RateLimitError): await sdk.chat.completions.create(model='synthetic', messages=[])
    assert len(sent) == 1 and r.http.gates['agent-model'].active == 0


async def test_search_request_capacity_cancel_releases_permit(monkeypatch):
    high_submit(monkeypatch, 'RAG_SEARCH')
    monkeypatch.setenv('RAG_SEARCH_UNFINISHED_CAPACITY', '1')
    started, release = asyncio.Event(), asyncio.Event()
    async def first():
        async with AdmissionStore().request('search', OperationDeadline.after(10)):
            started.set(); await release.wait()
    # RAG role is required by the scope policy.
    from tests.native_async import runtimes
    with bind(runtimes.get()['rag']):
        task = asyncio.create_task(first()); await started.wait()
        with pytest.raises(ApiError):
            async with AdmissionStore().request('search', OperationDeadline.after(10)): pass
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert query("SELECT count(*) AS n FROM execution.request_permits WHERE scope='search'")[0]['n'] == 0
        async with AdmissionStore().request('search', OperationDeadline.after(10)): pass


@pytest.mark.parametrize('kind', list(TaskKind))
async def test_claim_alternates_new_and_recovery_and_keeps_deadline(kind):
    from tests.native_async import runtimes
    r = runtimes.get()['rag' if kind == TaskKind.DOCUMENT else 'agent']
    ids = []
    with bind(r):
        for recovery in (False, True, False, True):
            key = uuid4(); ids.append(key)
            query('''INSERT INTO execution.tasks(id,kind,max_attempts,started_at,execution_deadline_at,execution_seconds)
                VALUES (%s,%s,3,CASE WHEN %s THEN clock_timestamp()-interval '20 seconds' END,
                CASE WHEN %s THEN clock_timestamp()+interval '200 seconds' END,CASE WHEN %s THEN 300 END)''', (key, kind, recovery, recovery, recovery))
        deadlines = {row['id']: row['execution_deadline_at'] for row in query('SELECT id,execution_deadline_at FROM execution.tasks')}
        for key in ids:
            async with transaction(profile='control') as conn:
                row = await store.claim(conn, kind, lease_seconds=60)
                assert row['id'] == key
                if deadlines[key]: assert row['execution_deadline_at'] == deadlines[key]
                await store.settle_failure(conn, TaskRef(key, kind), row['lease_token'], 'TEST_DONE')


async def test_claim_cursor_does_not_advance_on_rollback():
    rid = insert_run()
    with pytest.raises(ValueError):
        async with transaction(profile='control') as conn:
            row = await store.claim(conn, TaskKind.AGENT, lease_seconds=60)
            assert row['id'] == rid
            raise ValueError('rollback')
    assert query('SELECT count(*) AS n FROM execution.claim_cursors')[0]['n'] == 0
    assert query('SELECT attempts FROM execution.tasks WHERE id=%s', (rid,))[0]['attempts'] == 0


async def test_tools_receive_private_state_and_merge_in_graph_order():
    rid, token = await claim(); state = RunExecutionState(rid, token)
    message = AIMessage(content='', tool_calls=[{'name': 'probe', 'id': 'first', 'args': {}}, {'name': 'probe', 'id': 'second', 'args': {}}])
    shared = {'messages': [message], 'private': {'value': []}}
    second_done = asyncio.Event()
    def request(identity):
        return ToolCallRequest(tool_call={'name': 'probe', 'args': {'nested': []}, 'id': identity, 'type': 'tool_call'}, tool=None, state=shared,
            runtime=ToolRuntime(context=state, state=shared, config={}, stream_writer=lambda _: None, tool_call_id=identity, store=None))
    async def handler(req):
        identity = req.tool_call['id']
        req.runtime.state['private']['value'].append(identity)
        req.tool_call['args']['nested'].append(identity)
        if identity == 'first': await second_done.wait()
        else: second_done.set()
        result = ToolOutcome(identity, notices=({'code': 'N', 'message': identity},))
        return ToolMessage(content=identity, artifact=result.to_artifact(), tool_call_id=identity)
    requests=[request(identity) for identity in ('first','second')]
    for req in requests: await prepare_tool(req)
    await asyncio.gather(*(budgeted_tool.awrap_tool_call(req,handler) for req in requests))
    assert shared['private']['value'] == []
    assert state.notices == [{'code': 'N', 'message': 'first'}]
    assert query('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))[0]['tool_calls_used'] == 2


async def test_tool_run_and_worker_limits():
    r = resources(); entered = []; ready, release = asyncio.Event(), asyncio.Event()
    owners = [WorkOwner(TaskRef(uuid4(), TaskKind.AGENT), uuid4()) for _ in range(2)]
    async def run(owner):
        async with tool_slot(replace(owner, call_id=uuid4()), OperationDeadline.after(5)):
            entered.append(owner.attempt)
            if len(entered) == 4: ready.set()
            await release.wait()
    tasks = [asyncio.create_task(run(owner)) for owner in owners for _ in range(3)]
    await asyncio.wait_for(ready.wait(), 3)
    assert r.tool_gate.active == 4
    assert all(entered.count(owner.attempt) == 2 for owner in owners)
    release.set(); await asyncio.gather(*tasks)
    assert r.tool_gate.active == 0


async def test_cancelled_tool_keeps_slot_until_thread_really_finishes(monkeypatch):
    monkeypatch.setenv('AGENT_TOOL_CONCURRENCY', '1')
    r = resources(); started = threading.Event(); release = threading.Event()
    owner = WorkOwner(TaskRef(uuid4(), TaskKind.AGENT), uuid4(), uuid4())
    def block(): started.set(); release.wait(5)
    async def run():
        async with tool_slot(owner, OperationDeadline.after(8)):
            await r.io.run(block, owner=owner, deadline=OperationDeadline.after(8))
    task = asyncio.create_task(run())
    while not started.is_set(): await asyncio.sleep(.001)
    task.cancel(); await asyncio.sleep(.02)
    assert r.tool_gate.active == 1 and r.registry.unfinished_owner(owner) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert r.tool_gate.active == 0


async def test_oversized_tool_batch_rejected_before_any_tool_reservation():
    rid, token = await claim()
    state = RunExecutionState(rid, token)
    message = AIMessage(content='', tool_calls=[{'name': 'probe', 'args': {}, 'id': str(i)} for i in range(11)])
    request = SimpleNamespace(runtime=SimpleNamespace(context=state), state={'messages': []}, messages=[], system_message=None, tools=[], override=lambda **kwargs:request, model=None)
    async def handler(req): return SimpleNamespace(result=[message])
    with pytest.raises(ToolBatchTooLarge): await budgeted_model.awrap_model_call(request, handler)
    row = query('SELECT model_calls_used,tool_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))[0]
    assert row == {'model_calls_used': 1, 'tool_calls_used': 0}


async def test_progress_expired_activity_not_hidden_by_heartbeat_or_other_completion():
    rid, token = await claim(); ref = AttemptContext(TaskRef(rid, TaskKind.AGENT), token)
    slow, fast = uuid4(), uuid4(); progress = ProgressStore()
    await progress.begin(ref, slow, 'http:agent-model', OperationDeadline.after(120))
    await progress.begin(ref, fast, 'tool:other', OperationDeadline.after(120))
    await progress.finish(ref, fast)
    assert await store.renew(ref.task, token, 120)
    query("UPDATE execution.task_activities SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=%s", (slow,))
    assert await progress.inspect(ref.task, token)
    assert query('SELECT status,error_code,termination_reason FROM execution.tasks WHERE id=%s', (rid,))[0] == {
        'status': 'failed', 'error_code': 'EXECUTION_STALLED', 'termination_reason': 'progress_timeout'}
    assert not await store.renew(ref.task, token, 120)


async def test_progress_with_unconfirmed_external_call_is_unknown():
    rid, token = await claim(); ref = AttemptContext(TaskRef(rid, TaskKind.AGENT), token)
    await runtime.reserve_model(rid, token, 'react', 'synthetic')
    await ProgressStore().begin(ref, uuid4(), 'model', OperationDeadline.after(120))
    query("UPDATE execution.task_activities SET deadline_at=clock_timestamp()-interval '1 second'")
    assert await ProgressStore().inspect(ref.task, token)
    assert query('SELECT error_code FROM execution.tasks WHERE id=%s', (rid,))[0]['error_code'] == 'INTERRUPTED_UNKNOWN'


@pytest.mark.parametrize('change', ['cancel', 'takeover', 'terminal', 'lease', 'deadline'])
async def test_progress_monitor_never_overwrites_control_or_completed_result(change):
    rid, token = await claim(); ref = AttemptContext(TaskRef(rid, TaskKind.AGENT), token)
    await ProgressStore().begin(ref, uuid4(), 'slow', OperationDeadline.after(120))
    query("UPDATE execution.task_activities SET deadline_at=clock_timestamp()-interval '1 second'")
    assignments = {'cancel': "status='stopping',stop_requested_at=clock_timestamp()", 'takeover': 'lease_token=%s',
                   'terminal': "status='completed',lease_token=NULL,leased_until=NULL", 'lease': "leased_until=clock_timestamp()-interval '1 second'",
                   'deadline': "execution_deadline_at=clock_timestamp()-interval '1 second'"}
    query('UPDATE execution.tasks SET ' + assignments[change] + ' WHERE id=%s', (uuid4(), rid) if change=='takeover' else (rid,))
    before = query('SELECT status,lease_token,error_code FROM execution.tasks WHERE id=%s', (rid,))[0]
    assert not await ProgressStore().inspect(ref.task, token)
    assert query('SELECT status,lease_token,error_code FROM execution.tasks WHERE id=%s', (rid,))[0] == before


async def test_progress_duplicate_begin_never_extends_deadline_and_cpu_phase_is_not_idle():
    rid, token = await claim(); ref = AttemptContext(TaskRef(rid, TaskKind.AGENT), token)
    identity = uuid4(); progress = ProgressStore()
    await progress.begin(ref, identity, 'cpu:parse', OperationDeadline.after(200))
    before = query('SELECT deadline_at FROM execution.task_activities')[0]['deadline_at']
    await progress.begin(ref, identity, 'cpu:parse', OperationDeadline.after(250))
    query("UPDATE execution.tasks SET progress_at=clock_timestamp()-interval '80 seconds' WHERE id=%s", (rid,))
    assert not await progress.inspect(ref.task, token)
    assert query('SELECT deadline_at FROM execution.task_activities')[0]['deadline_at'] == before


async def test_roles_cannot_write_other_service_progress_or_capacity():
    from tests.native_async import runtimes
    rid, token = await claim()
    with bind(runtimes.get()['rag']):
        async with transaction(readonly=True) as conn:
            assert not await (await conn.execute('SELECT * FROM execution.task_activities WHERE task_id=%s', (rid,))).fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with transaction() as conn: await conn.execute('SELECT * FROM execution.capacity_reservations')


async def test_late_parallel_429_preserves_longer_server_cooldown():
    governor = DownstreamGovernor()
    first, _ = await governor.acquire('embedding', 'http://parallel.local', 10)
    second, _ = await governor.acquire('embedding', 'http://parallel.local', 10)
    await governor.feedback(first, status=429, retry_after='10')
    await governor.feedback(second, status=429, retry_after='120')
    row = query("SELECT cooldown_until-clock_timestamp() AS remaining,rate_factor FROM execution.flow_buckets WHERE key=%s", (first.key,))[0]
    assert row['remaining'].total_seconds() > 115 and row['rate_factor'] == .25
    await governor.feedback(first, status=200)
    assert query('SELECT unhealthy_since FROM execution.flow_buckets WHERE key=%s', (first.key,))[0]['unhealthy_since'] is not None


async def test_parallel_server_failures_trip_shared_cooling():
    governor = DownstreamGovernor()
    permits = []
    for _ in range(3):
        permit, _ = await governor.acquire('embedding', 'http://failures.local', 10)
        if permit is None:
            query("UPDATE execution.flow_buckets SET sampled_at=clock_timestamp()-interval '5 seconds'")
            permit, _ = await governor.acquire('embedding', 'http://failures.local', 10)
        permits.append(permit)
    for permit in permits: await governor.feedback(permit, status=503)
    row = query('SELECT failures,unhealthy_since FROM execution.flow_buckets WHERE key=%s', (permits[0].key,))[0]
    assert row['failures'] == 3 and row['unhealthy_since'] is not None


async def test_pressure_waiting_never_dispatches_after_unknown_permission(monkeypatch):
    from execution.persistence import PersistenceOutcomeUnknown
    from execution.infra.database import AsyncDatabase
    from contextlib import asynccontextmanager
    sent = []
    r = resources(); r.http.transports['embedding'] = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200,json={}))
    original = AsyncDatabase.transaction
    @asynccontextmanager
    async def lost_ack(self, *args, **kwargs):
        async with original(self, *args, **kwargs) as conn: yield conn
        raise PersistenceOutcomeUnknown('synthetic lost permission acknowledgement')
    monkeypatch.setattr(AsyncDatabase, 'transaction', lost_ack)
    with pytest.raises(PersistenceOutcomeUnknown):
        await r.http.request('embedding','POST','http://unknown.local/v1/embeddings',deadline=OperationDeadline.after(5))
    assert sent == [] and r.http.gates['embedding'].active == 0


async def test_api_can_report_admission_without_consuming_recovery_probe(monkeypatch):
    high_submit(monkeypatch)
    governor = DownstreamGovernor()
    permit, _ = await governor.acquire('agent-model', 'http://required.local', 10)
    query('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (uuid4(),))
    await governor.feedback(permit, status=429, retry_after='1')
    query("UPDATE execution.flow_buckets SET unhealthy_since=clock_timestamp()-interval '40 seconds',cooldown_until=clock_timestamp()-interval '1 second' WHERE scope='downstream'")
    store_api = AdmissionStore()
    first = await store_api.status('agent_run'); second = await store_api.status('agent_run')
    assert first['accepting'] and second['accepting']
    assert query("SELECT probe_until FROM execution.flow_buckets WHERE scope='agent_run'")[0]['probe_until'] is None
    await create_run(RunCreate(task='probe'), 'probe')
    with pytest.raises(ApiError) as error: await create_run(RunCreate(task='wait'), 'wait')
    assert error.value.status == 503


async def test_cold_claim_sweeps_expired_tasks_without_advancing_fairness():
    governor = DownstreamGovernor()
    permit, _ = await governor.acquire('agent-model', 'http://required.local', 10)
    await governor.feedback(permit, status=429, retry_after='60')
    expired = insert_run(queue_delta=timedelta(seconds=-1))
    valid = insert_run()
    assert await runtime.claim_run() is None
    assert query('SELECT status FROM execution.tasks WHERE id=%s', (expired,))[0]['status'] == 'failed'
    assert query('SELECT attempts FROM execution.tasks WHERE id=%s', (valid,))[0]['attempts'] == 0
    assert query('SELECT count(*) AS n FROM execution.claim_cursors')[0]['n'] == 0


async def test_tool_capacity_waiter_is_tracked_and_cancelled_without_budget(monkeypatch):
    monkeypatch.setenv('AGENT_TOOL_CONCURRENCY', '1')
    rid, token = await claim(); state = RunExecutionState(rid, token)
    shared = {'messages':[AIMessage(content='',tool_calls=[{'name':'probe','args':{},'id':i} for i in ('one','two')])]}; entered = asyncio.Event(); block = asyncio.Event()
    def request(identity):
        return ToolCallRequest(tool_call={'name':'probe','args':{},'id':identity,'type':'tool_call'},tool=None,state=shared,
            runtime=ToolRuntime(context=state,state=shared,config={},stream_writer=lambda _: None,tool_call_id=identity,store=None))
    async def handler(req):
        entered.set(); await block.wait()
        return ToolMessage(content='x',artifact=ToolOutcome('x').to_artifact(),tool_call_id=req.tool_call['id'])
    from execution.scope import stop_children
    with attempt(TaskRef(rid,TaskKind.AGENT),token) as ref:
        req1,req2=request('one'),request('two')
        await prepare_tool(req1);await prepare_tool(req2)
        first = asyncio.create_task(budgeted_tool.awrap_tool_call(req1,handler))
        await entered.wait()
        second = asyncio.create_task(budgeted_tool.awrap_tool_call(req2,handler))
        async with asyncio.timeout(3):
            while len(ref.children) < 2: await asyncio.sleep(.001)
        await stop_children()
        from execution.persistence import PersistenceOutcomeUnknown
        assert first.cancelled()
        assert second.cancelled() or isinstance(second.exception(), PersistenceOutcomeUnknown)
    assert query('SELECT tool_calls_used FROM agent.agent_runs WHERE id=%s',(rid,))[0]['tool_calls_used'] == 1


async def test_call_identity_is_inherited_by_managed_work():
    from execution.calls import CallContext,CallRunner,ProviderReply
    from execution.scope import work_owner
    from execution.persistence import Applied
    rid,token=await claim(); context=CallContext(TaskRef(rid,TaskKind.AGENT),token,OperationDeadline.after(10))
    async def invoke(call):
        assert work_owner() == call.owner
        return ProviderReply('ok')
    with attempt(context.task,context.token):
        await CallRunner().run(context,reserve=lambda key: _applied(key),invoke=invoke,
            settle=lambda *args,**kwargs: _applied(None),failure=lambda exc: ('FAILED',{},exc))
        assert work_owner().call_id is None


async def _applied(value):
    from execution.persistence import Applied
    return Applied(value)


async def test_current_worker_tool_policy_is_frozen(monkeypatch):
    from agent_service.startup import prepare
    r = resources(); r.agent_settings = prepare()
    monkeypatch.setenv('AGENT_TOOL_BATCH_LIMIT','100')
    state = RunExecutionState(uuid4(),uuid4())
    assert state.tool_policy['batch'] == 10


async def test_waiting_reports_do_not_fake_progress_or_extend_phase():
    rid,token=await claim(); ref=AttemptContext(TaskRef(rid,TaskKind.AGENT),token); identity=uuid4(); progress=ProgressStore()
    await progress.begin(ref,identity,'http:agent-model',OperationDeadline.after(120))
    before=query('SELECT progress_at,progress_seq FROM execution.tasks WHERE id=%s',(rid,))[0]
    await progress.waiting(ref,identity,'downstream_cooldown')
    await progress.waiting(ref,identity,'downstream_cooldown')
    assert query('SELECT progress_at,progress_seq FROM execution.tasks WHERE id=%s',(rid,))[0] == before
    async with transaction(readonly=True) as conn: observed=await progress.snapshot(conn,rid)
    assert observed['waiting_reason']=='downstream_cooldown'


async def test_owner_policy_configuration_preserves_cooldown_and_refuses_online_worker():
    from db.flow_control import configure
    governor=DownstreamGovernor(); permit,_=await governor.acquire('embedding',os.environ['RAG_EMBEDDING_URL'],10)
    await governor.feedback(permit,status=429,retry_after='60')
    before=query('SELECT cooldown_until FROM execution.flow_buckets WHERE key=%s',(permit.key,))[0]['cooldown_until']
    with connect('MIGRATION_DATABASE_URL') as conn: configure(conn)
    assert query('SELECT cooldown_until FROM execution.flow_buckets WHERE key=%s',(permit.key,))[0]['cooldown_until']==before
    query('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)',(uuid4(),))
    with pytest.raises(RuntimeError,match='Stop API'):
        with connect('MIGRATION_DATABASE_URL') as conn: configure(conn)


async def test_recovery_rate_gradually_returns_to_configured_ceiling():
    governor=DownstreamGovernor(); permit,_=await governor.acquire('embedding','http://recovery.local',10)
    await governor.feedback(permit,status=429,retry_after='1')
    query("UPDATE execution.flow_buckets SET cooldown_until=clock_timestamp()-interval '1 second'")
    for _ in range(60):
        query("UPDATE execution.flow_buckets SET sampled_at=clock_timestamp()-interval '30 seconds'")
        current,_=await governor.acquire('embedding','http://recovery.local',10)
        assert current is not None
        await governor.feedback(current,status=200)
    row=query('SELECT rate_factor,unhealthy_since FROM execution.flow_buckets WHERE key=%s',(permit.key,))[0]
    assert row['rate_factor']==1 and row['unhealthy_since'] is None


async def test_distinct_processes_share_request_tokens(monkeypatch):
    import sys
    monkeypatch.setenv('DOWNSTREAM_REQUEST_RATE','.0001')
    script = '''import asyncio,json,sys
from execution.infra.runtime import RuntimeResources
from execution.persistence import bind
from execution.flow import DownstreamGovernor
async def main():
 async with RuntimeResources(sys.argv[1]) as runtime:
  with bind(runtime):
   permit,_=await DownstreamGovernor().acquire('embedding','http://process.local',10)
   print(json.dumps(bool(permit)))
asyncio.run(main())'''
    processes=[await asyncio.create_subprocess_exec(sys.executable,'-c',script,role,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE) for role in ('agent-api','rag-api','agent-api')]
    results=await asyncio.gather(*(p.communicate() for p in processes))
    assert all(p.returncode==0 for p in processes)
    import json
    assert sum(json.loads(out) for out,err in results)==2


async def test_upload_rejection_happens_before_body_is_read():
    from execution.upload_admission import UploadAdmission
    from tests.native_async import runtimes
    r=runtimes.get()['rag']; called=[]
    async def app(scope,receive,send):
        called.append('app'); await receive()
    async def receive():
        called.append('body'); return {'type':'http.request','body':b'large upload','more_body':False}
    messages=[]
    async def send(message): messages.append(message)
    with bind(r):
        async with AdmissionStore().request('upload',OperationDeadline.after(10)):
            async with AdmissionStore().request('upload',OperationDeadline.after(10)):
                await UploadAdmission(app)({'type':'http','method':'POST','path':'/v1/documents'},receive,send)
    assert called==[]
    assert messages[0]['status']==429


async def test_cpu_phase_timeout_is_terminal_instead_of_retrying_parse(tmp_path):
    from tests.native_async import runtimes
    from tests.resource_jobs import identity,wait_file
    from execution.errors import ExecutionRevoked
    from execution.scope import work_owner
    r=runtimes.get()['rag']; r.config=replace(r.config,cleanup_grace=.02)
    with bind(r):
        await r.cpu.run(identity,'warm',deadline=OperationDeadline.after(5))
        task=TaskRef(uuid4(),TaskKind.DOCUMENT)
        async with transaction(profile='control') as conn:
            await store.create(conn,task)
            job=await store.claim(conn,task.kind,lease_seconds=60)
        with attempt(task,job['lease_token']):
            with pytest.raises(ExecutionRevoked,match='progress deadline'):
                await r.cpu.run(wait_file,str(tmp_path/'release'),owner=work_owner(),deadline=OperationDeadline.after(.1))
    row=query('SELECT status,error_code,termination_reason FROM execution.tasks WHERE id=%s',(task.id,))[0]
    assert row=={'status':'failed','error_code':'EXECUTION_STALLED','termination_reason':'progress_timeout'}


async def test_tools_with_injected_graph_state_are_rejected(monkeypatch):
    from typing import Annotated
    from langgraph.prebuilt import InjectedState
    from langchain_core.tools import StructuredTool
    from agent_service.tooling import ToolProvider,ToolDeclaration,load_providers
    from agent_service.tools import rag
    async def unsafe(state:Annotated[dict,InjectedState()]): return 'x',ToolOutcome('x').to_artifact()
    tool=StructuredTool.from_function(coroutine=unsafe,name='unsafe',description='unsafe fixture',response_format='content_and_artifact')
    monkeypatch.setattr(rag,'tools',lambda:ToolProvider('unsafe','1',(ToolDeclaration(tool,'1','read_only',False),),1))
    with pytest.raises(RuntimeError,match='injected graph storage'): load_providers('agent_service.tools.rag:tools')


async def test_declared_invalid_input_probe_restores_transport_availability():
    governor=DownstreamGovernor(); permit,_=await governor.acquire('embedding','http://input.local',10)
    await governor.feedback(permit,status=503)
    await governor.feedback(permit,status=503)
    await governor.feedback(permit,status=503)
    query("UPDATE execution.flow_buckets SET cooldown_until=clock_timestamp()-interval '1 second',sampled_at=clock_timestamp()-interval '10 seconds'")
    probe,_=await governor.acquire('embedding','http://input.local',10)
    await governor.feedback(probe,status=422)
    assert query('SELECT unhealthy_since FROM execution.flow_buckets WHERE key=%s',(permit.key,))[0]['unhealthy_since'] is None


async def test_optional_rag_branch_waits_for_healthy_quota_instead_of_degrading(monkeypatch):
    from execution.infra.http import fail_fast_pressure
    monkeypatch.setenv('DOWNSTREAM_REQUEST_RATE','5')
    monkeypatch.setenv('DOWNSTREAM_REQUEST_BURST','1')
    r=resources(); sent=[]
    async def server(request):
        sent.append(request); return httpx.Response(200,json={})
    r.http.transports['embedding']=httpx.MockTransport(server)
    marker=fail_fast_pressure.set(True)
    try:
        await r.http.request('embedding','POST','http://healthy.local',deadline=OperationDeadline.after(2))
        started=asyncio.get_running_loop().time()
        await r.http.request('embedding','POST','http://healthy.local',deadline=OperationDeadline.after(2))
        assert asyncio.get_running_loop().time()-started>.1
        assert len(sent)==2
    finally:fail_fast_pressure.reset(marker)


async def test_cpu_retirement_releases_named_semaphores_before_signal_exit():
    from pathlib import Path
    from tests.resource_jobs import identity
    from execution.infra.runtime import RuntimeResources
    async with RuntimeResources('agent-api') as r:
        await r.cpu.run(identity,'warm',deadline=OperationDeadline.after(5))
        def names(backend):
            manager=backend.manager
            return {lock._semlock.name for lock in (manager.call_queue._rlock,manager.call_queue._wlock,
                manager.call_queue._sem,manager.result_queue._rlock,manager.result_queue._wlock)}
        semaphores=names(r.cpu.pool)
        assert all(Path('/dev/shm/sem.'+name[1:]).exists() for name in semaphores)
    assert r.cpu.pool.manager is None
    assert all(not Path('/dev/shm/sem.'+name[1:]).exists() for name in semaphores)


@pytest.mark.parametrize('mode',['offline','cooling','queue'])
async def test_queued_run_explains_worker_or_downstream_wait(mode):
    rid=insert_run()
    if mode!='offline': query('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)',(uuid4(),))
    if mode=='cooling':
        governor=DownstreamGovernor(); permit,_=await governor.acquire('agent-model','http://cooling.local',10)
        await governor.feedback(permit,status=429,retry_after='60')
    async with transaction(readonly=True) as conn: info=await ProgressStore().snapshot(conn,rid)
    assert info['waiting_reason']=={'offline':'worker_unavailable','cooling':'downstream_cooldown','queue':'queued'}[mode]


async def test_cpu_reset_failure_is_a_valid_public_response():
    from contracts.v1 import RunResponse
    from agent_service.storage.runs import get_run
    rid,token=await claim()
    assert await runtime.fail_run(rid,token,'CPU_POOL_RESET')
    response=RunResponse.model_validate(await get_run(rid))
    assert response.error.code=='CPU_POOL_RESET' and response.status=='failed'
