from tests.acceptance_fixtures import fixture_publish_result
"""C01/C02/C03: compatibility, acknowledged rollback, and uncertain commits."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import os
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg.transaction import AsyncTransaction
import pytest

from agent_service import manifest, startup, runtime, runner
from agent_service.storage import runtime as runtime_store
from execution import retry, inputs
from execution.call_store import AgentCallStore
from execution.calls import CallContext, CallRunner, ProviderReply
from execution.contracts import TaskKind, TaskRef
from execution.errors import ExecutionRevoked
from execution.infra.types import OperationDeadline, ResourceClosed
from execution.persistence import transaction, resources, Applied, AlreadyApplied, PersistenceOutcomeUnknown, bind
from execution.reconciliation import OperationIdentity, ResolutionState
from tests.helpers import insert_run
from db.connection import connect

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


async def _claim():
    rid = insert_run()
    job = await runtime.claim_run()
    return rid, job['lease_token']


@pytest.mark.parametrize('change', ['legacy', 'state', 'prompt', 'graph', 'tools', 'digest', 'schema'])
async def test_recovery_rejects_incompatible_manifest_and_preserves_history(change):
    rid, token = await _claim()
    await runtime.attempt_exited(rid, token)
    with connect('MIGRATION_DATABASE_URL') as conn:
        row = conn.execute('SELECT schema_version,manifest FROM agent.run_manifests WHERE run_id=%s', (rid,)).fetchone()
        saved = row['manifest']
        schema = row['schema_version']
        if change == 'legacy': saved.pop('recovery_contract'); schema = 3
        elif change == 'state': saved['recovery_contract']['state_schema_version'] = 'old-state'
        elif change == 'prompt': saved['agent']['prompt_set_version'] = 'old-prompts'
        elif change == 'graph': conn.execute("UPDATE agent.agent_runs SET graph_version='old-graph' WHERE id=%s", (rid,))
        elif change == 'tools': saved['tools']['providers'][0]['contract_version'] = 0
        elif change == 'digest': saved['recovery_contract']['sha256'] = '0' * 64
        else: schema = 3
        conn.execute('UPDATE agent.run_manifests SET schema_version=%s,manifest=%s WHERE run_id=%s', (schema, Jsonb(saved), rid))
    assert await runtime.claim_run() is None
    async with transaction(readonly=True) as conn:
        state = await (await conn.execute('SELECT status,error_code,termination_reason,model_calls_used,tool_calls_used FROM agent.run_state WHERE id=%s', (rid,))).fetchone()
        history = await (await conn.execute('SELECT schema_version,manifest FROM agent.run_manifests WHERE run_id=%s', (rid,))).fetchone()
    assert state == dict(status='failed', error_code='INTERRUPTED_UNKNOWN', termination_reason='configuration_mismatch', model_calls_used=0, tool_calls_used=0)
    assert history == {'schema_version': schema, 'manifest': saved}


async def test_direct_execution_validates_before_input_or_model_work(monkeypatch):
    rid, token = await _claim()
    with connect('MIGRATION_DATABASE_URL') as conn:
        conn.execute("UPDATE agent.run_manifests SET manifest=jsonb_set(manifest,'{agent,prompt_set_version}',%s::jsonb) WHERE run_id=%s", ('"obsolete"', rid))
    async def forbidden(*args): pytest.fail('Incompatible run must not prepare inputs or call a provider')
    monkeypatch.setattr(inputs, 'prepare_inputs', forbidden)
    with pytest.raises(ExecutionRevoked): await runner.execute_run(rid, token)
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute('SELECT status,termination_reason FROM execution.tasks WHERE id=%s', (rid,))).fetchone()
    assert row == {'status': 'failed', 'termination_reason': 'configuration_mismatch'}


@pytest.mark.parametrize('change', ['schema', 'description', 'dependency', 'source'])
def test_recovery_fingerprint_catches_drift_without_declared_version_bump(monkeypatch, tmp_path, change):
    from agent_service.tooling import load_providers
    providers = load_providers()
    before = manifest.recovery_contract(providers)
    if change in {'schema', 'description'}:
        spec, provider = providers[0]
        declaration = provider.tools[0]
        updates = {'description': declaration.tool.description + ' changed'} if change == 'description' else {
            'args_schema': {'type': 'object', 'properties': {'query': {'type': 'integer'}}, 'required': ['query']}}
        updated = replace(declaration, tool=declaration.tool.model_copy(update=updates))
        providers = [(spec, replace(provider, tools=(updated, *provider.tools[1:]))), *providers[1:]]
        assert updated.version == declaration.version and provider.version == providers[0][1].version
    elif change == 'dependency':
        original = manifest.version
        monkeypatch.setattr(manifest, 'version', lambda name: original(name) + '.changed' if name == 'langgraph' else original(name))
    else:
        # Use a synthetic executable source root instead of editing the repo.
        path = tmp_path / 'agent_service'; path.mkdir()
        (path / 'execution.py').write_text('STATE_MEANING = "changed"\n')
        monkeypatch.setattr(manifest, 'REPOSITORY_ROOT', tmp_path)
    after = manifest.recovery_contract(providers)
    assert after['version'] == before['version'] and after['sha256'] != before['sha256']


async def test_real_deadlock_retries_whole_transaction_without_duplicate_effects():
    ids = [uuid4(), uuid4()]
    async with transaction() as conn:
        for key in ids: await conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s)', (key,))
        before = await (await conn.execute('SELECT instance_id,heartbeat_at FROM agent.worker_instances ORDER BY instance_id')).fetchall()
    ready = [asyncio.Event(), asyncio.Event()]
    attempts = [0, 0]
    @retry.retry_transaction
    async def update(index):
        attempts[index] += 1
        async with transaction() as conn:
            await conn.execute("UPDATE agent.worker_instances SET heartbeat_at=heartbeat_at+interval '1 second' WHERE instance_id=%s", (ids[index],))
            if attempts[index] == 1:
                ready[index].set()
                await ready[1-index].wait()
            await conn.execute("UPDATE agent.worker_instances SET heartbeat_at=heartbeat_at+interval '1 second' WHERE instance_id=%s", (ids[1-index],))
    async with asyncio.timeout(8): await asyncio.gather(update(0), update(1))
    async with transaction(readonly=True) as conn:
        after = await (await conn.execute('SELECT instance_id,heartbeat_at FROM agent.worker_instances ORDER BY instance_id')).fetchall()
    assert sorted(attempts) == [1, 2]
    assert all(a['heartbeat_at'] - b['heartbeat_at'] == timedelta(seconds=2) for a, b in zip(after, before))


async def test_real_repeatable_read_serialization_failure_retries_fresh_snapshot():
    key = uuid4(); read = asyncio.Event(); written = asyncio.Event(); attempts = 0
    async with transaction() as conn:
        before = (await (await conn.execute('INSERT INTO agent.worker_instances(instance_id) VALUES (%s) RETURNING heartbeat_at', (key,))).fetchone())['heartbeat_at']
    async def concurrent_writer():
        await read.wait()
        async with transaction() as conn:
            await conn.execute("UPDATE agent.worker_instances SET heartbeat_at=heartbeat_at+interval '1 second' WHERE instance_id=%s", (key,))
        written.set()
    @retry.retry_transaction
    async def update():
        nonlocal attempts
        attempts += 1
        async with transaction(isolation='repeatable read') as conn:
            row = await (await conn.execute('SELECT heartbeat_at FROM agent.worker_instances WHERE instance_id=%s', (key,))).fetchone()
            if attempts == 1: read.set(); await written.wait()
            await conn.execute('UPDATE agent.worker_instances SET heartbeat_at=%s WHERE instance_id=%s', (row['heartbeat_at'] + timedelta(seconds=1), key))
    async with asyncio.timeout(5): await asyncio.gather(update(), concurrent_writer())
    async with transaction(readonly=True) as conn:
        after = (await (await conn.execute('SELECT heartbeat_at FROM agent.worker_instances WHERE instance_id=%s', (key,))).fetchone())['heartbeat_at']
    assert attempts == 2 and after - before == timedelta(seconds=2)


@pytest.mark.parametrize('sqlstate', ['40001', '40P01'])
async def test_rollback_after_writes_retries_reservation_with_same_id(monkeypatch, sqlstate):
    rid, token = await _claim(); identity = uuid4(); visits = []
    original = runtime_store.connect
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def fail_once(*args, **kwargs):
        async with original(*args, **kwargs) as conn:
            class Proxy:
                async def execute(self, query, *params):
                    cursor = await conn.execute(query, *params)
                    if 'INSERT INTO agent.model_calls' in query:
                        visits.append(params[0][0])
                        if len(visits) == 1:
                            await conn.execute(f"DO $$ BEGIN RAISE EXCEPTION 'synthetic rollback' USING ERRCODE='{sqlstate}'; END $$")
                    return cursor
            yield Proxy()
    monkeypatch.setattr(runtime_store, 'connect', fail_once)
    assert await runtime.reserve_model(rid, token, 'react', 'mock', call_id=identity) == identity
    assert visits == [identity, identity]
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone())['model_calls_used'] == 1
        assert (await (await conn.execute('SELECT count(*) AS n FROM agent.model_calls WHERE run_id=%s', (rid,))).fetchone())['n'] == 1


async def test_retry_bound_jitter_and_cleanup_before_wait(monkeypatch):
    count = 0; delays = []
    original_sleep = asyncio.sleep
    async def sleep(seconds):
        assert 'db-active' not in resources().active.get(asyncio.current_task(), [])
        delays.append(seconds)
        await original_sleep(0)
    monkeypatch.setattr(retry.random, 'uniform', lambda lower, upper: upper)
    monkeypatch.setattr(retry, 'backoff_wait', sleep)
    @retry.retry_transaction
    async def failure():
        nonlocal count
        count += 1
        async with transaction() as conn:
            await conn.execute("DO $$ BEGIN RAISE EXCEPTION 'retryable' USING ERRCODE='40001'; END $$")
    with pytest.raises(retry.TransactionRolledBack): await failure()
    assert count == 3 and delays == [.025, .05]


async def test_retry_preserves_original_deadline_and_stops_on_cancel(monkeypatch):
    count = 0
    @retry.retry_transaction
    async def failure():
        nonlocal count
        count += 1
        async with transaction() as conn:
            await conn.execute("DO $$ BEGIN RAISE EXCEPTION 'retryable' USING ERRCODE='40P01'; END $$")
    monkeypatch.setattr(retry.random, 'uniform', lambda lower, upper: upper)
    limit = OperationDeadline.after(.01)
    with bind(resources(), deadline=limit), pytest.raises((retry.TransactionRolledBack, TimeoutError)):
        await failure()
    assert count <= 1
    started = asyncio.Event()
    async def waiting(seconds): started.set(); await asyncio.Event().wait()
    monkeypatch.setattr(retry, 'backoff_wait', waiting)
    task = asyncio.create_task(failure())
    async with asyncio.timeout(5):
        await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    assert count <= 2


@pytest.mark.parametrize('error', [psycopg.OperationalError('connection'), psycopg.errors.UniqueViolation(), ResourceClosed(), PersistenceOutcomeUnknown()])
async def test_no_retry_without_positive_transaction_rollback(error):
    seen = []
    async def operation(): seen.append(True); raise error
    with pytest.raises(type(error)): await retry.rolled_back_retry(operation)
    assert seen == [True]


def _lose_ack(monkeypatch, *, cancel=False, rollback=False):
    original = AsyncTransaction.__aexit__; fired = []
    async def uncertain(self, exc_type, exc, tb):
        if exc_type is None and not fired:
            fired.append(True)
            if rollback:
                failure = RuntimeError('rolled back before ack')
                await original(self, RuntimeError, failure, None)
            else:
                await original(self, exc_type, exc, tb)
            if cancel: raise asyncio.CancelledError()
            raise psycopg.OperationalError('commit acknowledgement lost')
        return await original(self, exc_type, exc, tb)
    monkeypatch.setattr(AsyncTransaction, '__aexit__', uncertain)
    return fired


@pytest.mark.parametrize('kind', ['model', 'tool'])
@pytest.mark.parametrize('cancel', [False, True])
async def test_unknown_reservation_exposes_stable_identity_and_never_dispatches(monkeypatch, kind, cancel):
    rid, token = await _claim(); _lose_ack(monkeypatch, cancel=cancel)
    reserve = runtime.reserve_model if kind == 'model' else runtime.reserve_tool
    args = (rid, token, 'react', 'mock') if kind == 'model' else (rid, token, 'probe', {})
    with pytest.raises(PersistenceOutcomeUnknown) as caught: await reserve(*args)
    error = caught.value
    assert error.operation.kind == 'reserve_' + kind and error.resolution.state == ResolutionState.RECORDED
    assert str(token) not in repr(error.operation)
    ledger = AgentCallStore(resources())
    again = await ledger.reserve_model(*args, call_id=error.operation.id) if kind == 'model' else await ledger.reserve_tool(*args, call_id=error.operation.id)
    assert isinstance(again, AlreadyApplied)
    async with transaction(readonly=True) as conn:
        row = await (await conn.execute(f'SELECT {kind}_calls_used AS n FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone()
    assert row['n'] == 1


@pytest.mark.parametrize('kind', ['model', 'tool'])
async def test_unknown_settlement_queries_committed_row_without_reinvoking_provider(monkeypatch, kind):
    rid, token = await _claim(); ledger = AgentCallStore(resources()); sent = []
    identity = CallContext(TaskRef(rid, TaskKind.AGENT), token, OperationDeadline.after(5))
    async def reserve(call_id):
        return await ledger.reserve_model(rid, token, 'react', 'mock', call_id=call_id) if kind == 'model' else await ledger.reserve_tool(rid, token, 'probe', {}, call_id=call_id)
    async def invoke(context):
        sent.append(True)
        _lose_ack(monkeypatch)
        return ProviderReply('actual output')
    async def settle(call_id, status, **metadata):
        return await ledger.settle_model(rid, token, call_id, status, **metadata) if kind == 'model' else await ledger.settle_tool(rid, token, call_id, status, **metadata)
    reply = await CallRunner().run(identity, reserve=reserve, invoke=invoke, settle=settle, failure=lambda e: ('FAIL', {}, e))
    assert reply.value == 'actual output' and sent == [True]
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute(f'SELECT status FROM agent.{kind}_calls WHERE id=%s', (identity.id,))).fetchone())['status'] == 'succeeded'


@pytest.mark.parametrize('operation', ['settle', 'publish', 'fail'])
@pytest.mark.parametrize('rollback', [False, True])
async def test_unknown_commit_has_explicit_applied_or_not_applied_postcondition(monkeypatch, operation, rollback):
    rid, token = await _claim()
    call_id = await runtime.reserve_model(rid, token, 'final', 'mock', final=True)
    _lose_ack(monkeypatch, rollback=rollback)
    async def act():
        if operation == 'settle': return await runtime.settle_model(rid, token, call_id, 'succeeded')
        if operation == 'fail': return await runtime.fail_run(rid, token, 'MODEL_CALL_FAILED')
        return await fixture_publish_result(rid, token, {'answer': 'final', 'claims': [], 'citations': [], 'notices': []}, {}, False, 'done')
    if rollback:
        with pytest.raises(PersistenceOutcomeUnknown) as caught: await act()
        assert caught.value.resolution.state == ResolutionState.NOT_APPLIED
    else:
        result = await act()
        assert result is True if operation == 'fail' else isinstance(result, AlreadyApplied)
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone())['model_calls_used'] == 1
        rows = await (await conn.execute('SELECT id FROM agent.run_results WHERE run_id=%s', (rid,))).fetchall()
        assert len(rows) == (1 if operation == 'publish' and not rollback else 0)


async def test_unknown_query_failure_stays_unresolved_and_does_not_replay(monkeypatch):
    rid, token = await _claim(); _lose_ack(monkeypatch)
    async def unavailable(*args): raise psycopg.OperationalError('verification unavailable')
    monkeypatch.setattr(runtime_store, 'reconcile_operation', unavailable)
    with pytest.raises(PersistenceOutcomeUnknown) as caught:
        await runtime.reserve_model(rid, token, 'react', 'mock')
    assert caught.value.operation.id and caught.value.resolution.state == ResolutionState.UNRESOLVED
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone())['model_calls_used'] == 1


async def test_reconciliation_waits_for_inflight_writer_before_reporting_absence():
    rid, token = await _claim(); call_id = uuid4(); attempted = asyncio.Event()
    operation = OperationIdentity('reserve_model', TaskRef(rid, TaskKind.AGENT), call_id, token)
    expected = dict(step_id=None, purpose='react', model='mock', input_summary={})
    pending = None
    try:
        async with transaction() as writer:
            await writer.execute('SELECT id FROM execution.tasks WHERE id=%s FOR UPDATE', (rid,))
            async def verify():
                attempted.set()
                return await runtime_store.reconcile_operation(operation, expected)
            pending = asyncio.create_task(verify())
            await attempted.wait()
            async with transaction(readonly=True) as monitor:
                async with asyncio.timeout(5):
                    while True:
                        blocked = await (await monitor.execute('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))) AS blocked', (writer.info.backend_pid,))).fetchone()
                        if blocked['blocked']: break
                        assert not pending.done(); await asyncio.sleep(0)
            await writer.execute("INSERT INTO agent.model_calls(id,run_id,purpose,model,status,input_summary,attempt_token) VALUES (%s,%s,'react','mock','started','{}',%s)", (call_id, rid, token))
        async with asyncio.timeout(5): result = await pending
        assert result.state == ResolutionState.RECORDED and result.value == call_id
    finally:
        if pending is not None and not pending.done(): pending.cancel(); await asyncio.gather(pending, return_exceptions=True)


async def test_nested_retry_operations_do_not_multiply_attempts():
    calls = []
    @retry.retry_transaction
    async def inner():
        calls.append(True)
        async with transaction() as conn:
            await conn.execute("DO $$ BEGIN RAISE EXCEPTION 'retryable' USING ERRCODE='40001'; END $$")
    @retry.retry_transaction
    async def outer(): return await inner()
    with pytest.raises(retry.TransactionRolledBack): await outer()
    assert calls == [True] * 3


async def test_acknowledged_commit_stage_serialization_abort_is_retryable_not_unknown(monkeypatch):
    rid, token = await _claim(); identity = uuid4()
    original = AsyncTransaction.__aexit__; fired = []
    async def abort_commit(self, exc_type, exc, tb):
        if exc_type is None and not fired:
            fired.append(True)
            failure = psycopg.errors.SerializationFailure('synthetic commit abort')
            # The DB actually rolls back and acknowledges it, unlike a lost
            # COMMIT response. Only then report the retryable SQLSTATE.
            await original(self, type(failure), failure, None)
            raise failure
        return await original(self, exc_type, exc, tb)
    monkeypatch.setattr(AsyncTransaction, '__aexit__', abort_commit)
    assert await runtime.reserve_model(rid, token, 'react', 'mock', call_id=identity) == identity
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone())['model_calls_used'] == 1
        assert (await (await conn.execute('SELECT count(*) AS n FROM agent.model_calls WHERE run_id=%s', (rid,))).fetchone())['n'] == 1


@pytest.mark.parametrize('kind', ['model', 'tool'])
async def test_uncommitted_reservation_is_known_absent_but_never_dispatched(monkeypatch, kind):
    rid, token = await _claim(); _lose_ack(monkeypatch, rollback=True)
    reserve = runtime.reserve_model if kind == 'model' else runtime.reserve_tool
    args = (rid, token, 'react', 'mock') if kind == 'model' else (rid, token, 'probe', {})
    with pytest.raises(PersistenceOutcomeUnknown) as caught: await reserve(*args)
    assert caught.value.resolution.state == ResolutionState.NOT_APPLIED
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute(f'SELECT {kind}_calls_used AS n FROM agent.agent_runs WHERE id=%s', (rid,))).fetchone())['n'] == 0


@pytest.mark.parametrize('problem', ['missing_fields', 'boolean_version', 'invalid_digest', 'missing_sources'])
def test_incomplete_recovery_descriptor_is_not_accepted(monkeypatch, tmp_path, problem):
    if problem == 'missing_sources':
        monkeypatch.setattr(manifest, 'REPOSITORY_ROOT', tmp_path)
        with pytest.raises(RuntimeError, match='executable source'): manifest.execution_manifest()
        return
    current = manifest.execution_manifest(); saved = deepcopy(current)
    if problem == 'missing_fields': saved['recovery_contract'].pop('state_schema_version')
    elif problem == 'boolean_version': saved['recovery_contract']['version'] = True
    else: saved['recovery_contract']['sha256'] = 'x' * 64
    assert not startup.compatible(saved, current)


async def test_cached_startup_manifest_does_not_bypass_current_contract_check(monkeypatch):
    rid, token = await _claim()
    settings = startup.prepare()
    resources().agent_settings = settings
    before = deepcopy(settings['manifest'])
    original = manifest.recovery_contract
    def changed(providers):
        value = original(providers)
        value['sha256'] = '0' * 64
        return value
    monkeypatch.setattr(manifest, 'recovery_contract', changed)
    with pytest.raises(ExecutionRevoked): await startup.validate_execution(rid, token)
    assert settings['manifest'] == before
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT termination_reason FROM execution.tasks WHERE id=%s', (rid,))).fetchone())['termination_reason'] == 'configuration_mismatch'


async def test_reconciliation_rejects_same_identifier_from_different_run():
    rid, token = await _claim()
    foreign = await runtime.reserve_model(rid, token, 'react', 'mock')
    other, other_token = await _claim()
    operation = OperationIdentity('reserve_model', TaskRef(other, TaskKind.AGENT), foreign, other_token)
    result = await runtime_store.reconcile_operation(operation, dict(step_id=None, purpose='react', model='mock', input_summary={}))
    assert result.state == ResolutionState.REJECTED
    async with transaction(readonly=True) as conn:
        assert (await (await conn.execute('SELECT model_calls_used FROM agent.agent_runs WHERE id=%s', (other,))).fetchone())['model_calls_used'] == 0
