"""Task authority. All mutations use the caller's transaction, never a second write connection."""
import os
from uuid import UUID, uuid4
from execution.contracts import FailureResult, TaskKind, TaskRef, LeaseSnapshot, TERMINAL
from execution.persistence import transaction as connect
from execution.errors import ExecutionRevoked
from execution.retry import retry_transaction
ENV = {TaskKind.AGENT: 'AGENT_DATABASE_URL', TaskKind.MEMORY: 'AGENT_DATABASE_URL', TaskKind.DOCUMENT: 'RAG_DATABASE_URL'}
TIMEOUT_CODE = {TaskKind.AGENT: 'RUN_TIMEOUT', TaskKind.MEMORY: 'MEMORY_TIMEOUT', TaskKind.DOCUMENT: 'PROCESSING_TIMEOUT'}
DEFAULT_SECONDS = {TaskKind.AGENT: 300, TaskKind.MEMORY: 900, TaskKind.DOCUMENT: 14400}
DURATION_ENV = {TaskKind.AGENT: 'AGENT_EXECUTION_TIMEOUT_SECONDS', TaskKind.MEMORY: 'AGENT_MEMORY_TIMEOUT_SECONDS', TaskKind.DOCUMENT: 'RAG_DOCUMENT_TIMEOUT_SECONDS'}


def authority_refusal(task: TaskRef, token: UUID, row: dict | None, now):
    """One decision rule for guarded writes and observational control checks."""
    if row is None:
        return 'missing'
    return LeaseSnapshot(TaskRef(row['id'], row['kind']), row['lease_token'], row['status'],
                         row['leased_until'], row['execution_deadline_at'],
                         row['stop_requested_at']).refusal(task, token, now)


def control_refusal(task: TaskRef, token: UUID, row: dict | None):
    """A control observation is not authorization for a subsequent write."""
    return authority_refusal(task, token, row, row['now'] if row is not None else None)

async def create(conn, task: TaskRef, *, queue_seconds: int | None=None, reservation_run_id=None) -> dict:
    from execution.persistence import resources
    resources().check()
    from execution.admission import AdmissionStore
    await AdmissionStore().accept_task(conn, task, reservation_run_id=reservation_run_id)
    return await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
        INSERT INTO execution.tasks(id,kind,max_attempts,created_at,updated_at,available_at,queue_deadline_at)
        SELECT %s,%s,%s,clock.at,clock.at,clock.at,CASE WHEN %s::integer IS NULL THEN NULL
            ELSE clock.at+(%s * interval '1 second') END FROM clock RETURNING *""",
        (task.id, task.kind, 2147483647 if task.kind == TaskKind.AGENT else 3, queue_seconds, queue_seconds))).fetchone()

async def assert_active(conn, task: TaskRef, token: UUID, *, lock: bool=True) -> dict:
    from execution.scope import local_authority
    local_authority(task, token)
    row = await (await conn.execute('SELECT *,clock_timestamp() AS observed_at FROM execution.tasks\n        WHERE id=%s AND kind=%s' + (' FOR UPDATE' if lock else ''), (task.id, task.kind))).fetchone()
    # Read wall-clock time after acquiring the lock. Transaction time and the
    # SELECT projection may predate a lock wait and cannot authorize a write.
    now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
    if authority_refusal(task, token, row, now) is not None:
        raise ExecutionRevoked('Execution authority has expired or been revoked')
    local_authority(task, token)
    row['observed_at'] = now
    return row

@retry_transaction(profile='control')
async def check(task: TaskRef, token: UUID) -> dict:
    async with connect(ENV[task.kind], profile='control') as conn:
        return await assert_active(conn, task, token)

@retry_transaction(profile='control')
async def control_snapshot(task: TaskRef):
    async with connect(ENV[task.kind], profile='control', readonly=True) as conn:
        return await (await conn.execute('SELECT *,clock_timestamp() AS now FROM execution.tasks WHERE id=%s AND kind=%s',
                                        (task.id, task.kind))).fetchone()

@retry_transaction(profile='control')
async def renew(task: TaskRef, token: UUID, seconds: int) -> bool:
    from execution.persistence import resources
    resources().check()
    if seconds <= 0:
        raise ValueError('Lease duration must be positive')
    try:
        async with connect(ENV[task.kind], profile='control') as conn:
            await assert_active(conn, task, token)
            row = await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
                UPDATE execution.tasks t SET leased_until=LEAST(execution_deadline_at,
                  clock.at+(%s * interval '1 second')),heartbeat_at=clock.at,updated_at=clock.at FROM clock
                WHERE t.id=%s AND t.kind=%s AND lease_token=%s AND status='running' AND stop_requested_at IS NULL
                  AND leased_until>clock.at AND execution_deadline_at>clock.at RETURNING t.id""", (seconds, task.id, task.kind, token))).fetchone()
        return bool(row)
    except PermissionError:
        return False

async def complete(conn, task: TaskRef, token: UUID, *, status='completed', reason=None) -> None:
    before = await assert_active(conn, task, token)
    from execution.admission import release_memory
    await release_memory(conn, task)
    row = await (await conn.execute("UPDATE execution.tasks SET status=%s,termination_reason=%s,error_code=NULL,\n        finished_at=clock_timestamp(),updated_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,worker_id=NULL\n        WHERE id=%s AND kind=%s AND lease_token=%s AND status='running'\n          AND stop_requested_at IS NULL AND leased_until>clock_timestamp()\n          AND execution_deadline_at>clock_timestamp() RETURNING id", (status, reason, task.id, task.kind, token))).fetchone()
    from execution.scope import local_authority
    local_authority(task, token)
    now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
    if not row or now >= before['leased_until'] or now >= before['execution_deadline_at']:
        raise ExecutionRevoked('Execution authority expired at publication')

async def stop(conn, task: TaskRef) -> dict | None:
    row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE', (task.id, task.kind))).fetchone()
    if not row or row['status'] in TERMINAL:
        return row
    queued = row['status'] == 'queued'
    if queued:
        from execution.admission import release_memory
        await release_memory(conn, task)
    return await (await conn.execute("UPDATE execution.tasks SET status=%s,stop_requested_at=COALESCE(stop_requested_at,clock_timestamp()),\n        stop_reason='user_cancel',updated_at=clock_timestamp(),\n        finished_at=CASE WHEN %s THEN clock_timestamp() ELSE finished_at END,\n        termination_reason=CASE WHEN %s THEN 'cancelled_before_start' ELSE termination_reason END\n        WHERE id=%s RETURNING *", ('cancelled' if queued else 'stopping', queued, queued, task.id))).fetchone()

async def _terminal(conn, task: TaskRef, status: str, code: str | None, reason: str) -> None:
    from execution.admission import release_memory
    await release_memory(conn, task)
    await conn.execute('UPDATE execution.tasks SET status=%s,error_code=%s,termination_reason=%s,\n        finished_at=clock_timestamp(),updated_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,worker_id=NULL\n        WHERE id=%s AND kind=%s', (status, code, reason, task.id, task.kind))

async def settle_failure(conn, task: TaskRef, token: UUID, code: str, *, retryable=False, reason: str | None=None, worker_exit=False) -> FailureResult:
    row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE', (task.id, task.kind))).fetchone()
    if not row or row['status'] not in ('running', 'stopping') or row['lease_token'] != token:
        return FailureResult(False)
    now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
    if row['stop_requested_at'] is not None or row['status'] == 'stopping':
        await _terminal(conn, task, 'cancelled', None, 'cancelled')
        return FailureResult(True, 'cancelled')
    if row['execution_deadline_at'] <= now:
        code = TIMEOUT_CODE[task.kind]
        await _terminal(conn, task, 'failed', code, 'deadline')
        return FailureResult(True, 'failed', code)
    if row['leased_until'] <= now and (not worker_exit):
        return FailureResult(False)
    if retryable and row['attempts'] < row['max_attempts']:
        delay = 10 if task.kind == TaskKind.DOCUMENT else 0
        if 'RATE_LIMITED' in code or 'UNAVAILABLE' in code:
            bindings = ['rag:embedding'] if task.kind == TaskKind.DOCUMENT else ['agent:agent-model','agent:embedding']
            until = (await (await conn.execute("""SELECT max(cooldown_until) AS at FROM execution.flow_buckets b
                JOIN execution.downstream_bindings d ON b.scope='downstream' AND b.key=d.group_key
                WHERE d.service=ANY(%s)""", (bindings,))).fetchone())['at']
            delay = max(delay, 5, (until-now).total_seconds() if until else 0)
        retried = await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
            UPDATE execution.tasks t SET status='queued',error_code=%s,termination_reason=%s,
            lease_token=NULL,leased_until=NULL,worker_id=NULL,updated_at=clock.at,
            available_at=clock.at+(%s * interval '1 second') FROM clock
            WHERE t.id=%s AND execution_deadline_at>clock.at RETURNING t.id""",
            (None if task.kind == TaskKind.AGENT else code, reason, delay, task.id))).fetchone()
        if retried:
            return FailureResult(True, 'queued', None if task.kind == TaskKind.AGENT else code)
        code = TIMEOUT_CODE[task.kind]
        await _terminal(conn, task, 'failed', code, 'deadline')
        return FailureResult(True, 'failed', code)
    await _terminal(conn, task, 'failed', code, reason or code.lower())
    return FailureResult(True, 'failed', code)

async def sweep(conn, kind: TaskKind):
    if kind == TaskKind.AGENT:
        from agent_service.runtime import safe_to_resume, interrupt_calls
    expired = await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
        SELECT t.* FROM execution.tasks t CROSS JOIN clock
        WHERE kind=%s AND status IN ('queued','running','stopping')
        AND ((queue_deadline_at<=clock.at AND started_at IS NULL)
          OR execution_deadline_at<=clock.at
          OR (status IN ('running','stopping') AND leased_until<=clock.at))
        ORDER BY created_at FOR UPDATE OF t SKIP LOCKED""", (kind,))).fetchall()
    for row in expired:
        task = TaskRef(row['id'], kind)
        status, code, reason = (None, None, None)
        # Recovery validation can take multiple queries. Sample actual time
        # afterwards so a task expiring during validation cannot be requeued.
        safe = True
        if kind == TaskKind.AGENT and row['started_at'] is not None and row['attempts'] < row['max_attempts'] and row['stop_requested_at'] is None and row['status'] != 'stopping':
            safe = await safe_to_resume(conn, row['id'])
        now = (await (await conn.execute('SELECT clock_timestamp() AS now')).fetchone())['now']
        if row['stop_requested_at'] is not None or row['status'] == 'stopping':
            status, reason = ('cancelled', 'cancelled')
        elif row['execution_deadline_at'] is not None and row['execution_deadline_at'] <= now:
            status, code, reason = ('failed', TIMEOUT_CODE[kind], 'deadline')
        elif row['started_at'] is None and row['queue_deadline_at'] is not None and row['queue_deadline_at'] <= now:
            status, code, reason = ('failed', 'QUEUE_TIMEOUT', 'queue_timeout')
        elif row['attempts'] >= row['max_attempts']:
            status, code, reason = ('failed', 'WORKER_INTERRUPTED', 'attempts_exhausted')
        elif row['started_at'] is None or row['leased_until'] is not None and row['leased_until'] > now:
            continue
        elif kind == TaskKind.AGENT and not safe:
            status, code, reason = ('failed', 'INTERRUPTED_UNKNOWN', 'interrupted_unknown')
        else:
            requeued = await (await conn.execute("UPDATE execution.tasks SET status='queued',lease_token=NULL,leased_until=NULL,worker_id=NULL,\n                available_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp()\n                WHERE id=%s AND execution_deadline_at>clock_timestamp() RETURNING id", (10 if kind == TaskKind.DOCUMENT else 0, row['id']))).fetchone()
            if not requeued:
                status, code, reason = ('failed', TIMEOUT_CODE[kind], 'deadline')
        if status:
            await _terminal(conn, task, status, code, reason)
            if kind == TaskKind.AGENT:
                await interrupt_calls(conn, row['id'], code)

async def claim(conn, kind: TaskKind, *, lease_seconds: int, worker_id: UUID | None=None, sweep_expired=True) -> dict | None:
    from execution.persistence import resources
    resources().check()
    if lease_seconds <= 0:
        raise ValueError('Lease duration must be positive')
    if sweep_expired:
        await sweep(conn, kind)
    bindings = ['rag:embedding'] if kind == TaskKind.DOCUMENT else ['agent:agent-model'] if kind == TaskKind.AGENT else ['agent:agent-model','agent:embedding']
    cooling = await (await conn.execute("""SELECT 1 FROM execution.flow_buckets b
        JOIN execution.downstream_bindings d ON b.scope='downstream' AND b.key=d.group_key
        WHERE d.service=ANY(%s) AND b.cooldown_until>clock_timestamp() LIMIT 1""", (bindings,))).fetchone()
    if cooling: return None
    await conn.execute('INSERT INTO execution.claim_cursors(kind) VALUES (%s) ON CONFLICT DO NOTHING', (kind,))
    cursor = await (await conn.execute('SELECT last_recovery FROM execution.claim_cursors WHERE kind=%s FOR UPDATE', (kind,))).fetchone()
    row = None
    for recovery in (not cursor['last_recovery'], cursor['last_recovery']):
        row = await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
            SELECT t.* FROM execution.tasks t CROSS JOIN clock WHERE kind=%s AND status='queued'
            AND available_at<=clock.at AND attempts<max_attempts AND (started_at IS NOT NULL)=%s
            AND (queue_deadline_at IS NULL OR started_at IS NOT NULL OR queue_deadline_at>clock.at)
            AND (execution_deadline_at IS NULL OR execution_deadline_at>clock.at)
            ORDER BY CASE WHEN %s THEN available_at ELSE created_at END,created_at,id
            FOR UPDATE OF t SKIP LOCKED LIMIT 1""", (kind, recovery, recovery))).fetchone()
        if row: break
    if not row:
        return None
    duration = int(os.environ.get(DURATION_ENV[kind], DEFAULT_SECONDS[kind]))
    if duration <= 0:
        raise ValueError('Execution duration must be positive')
    claimed = await (await conn.execute("""WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS at)
        UPDATE execution.tasks t SET status='running',attempts=attempts+1,error_code=NULL,termination_reason=NULL,
        lease_token=%s,worker_id=%s,started_at=COALESCE(started_at,clock.at),
        execution_seconds=COALESCE(execution_seconds,%s),
        execution_deadline_at=COALESCE(execution_deadline_at,clock.at+(%s * interval '1 second')),
        leased_until=LEAST(COALESCE(execution_deadline_at,clock.at+(%s * interval '1 second')),
                          clock.at+(%s * interval '1 second')),
        heartbeat_at=clock.at,updated_at=clock.at,progress_at=clock.at,progress_seq=progress_seq+1 FROM clock
        WHERE t.id=%s AND t.kind=%s AND t.status='queued' AND t.stop_requested_at IS NULL
          AND available_at<=clock.at AND attempts<max_attempts
          AND (queue_deadline_at IS NULL OR started_at IS NOT NULL OR queue_deadline_at>clock.at)
          AND (execution_deadline_at IS NULL OR execution_deadline_at>clock.at)
        RETURNING t.*,EXTRACT(EPOCH FROM execution_deadline_at-clock_timestamp()) AS remaining_seconds""",
        (uuid4(), worker_id, duration, duration, duration, lease_seconds, row['id'], kind))).fetchone()
    if claimed is None:
        # The locked candidate may have expired after selection. No token,
        # attempt, budget, or first-start time has been assigned to it.
        await sweep(conn, kind)
    else:
        await conn.execute('UPDATE execution.claim_cursors SET last_recovery=%s WHERE kind=%s', (recovery, kind))
    return claimed
