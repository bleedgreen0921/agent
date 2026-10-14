"""Trusted phase events and lock-protected progress timeout settlement."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from execution import store
from execution.contracts import TaskKind
from execution.errors import ExecutionRevoked
from execution.flow import alert
from execution.flow_config import number
from execution.persistence import transaction
from execution.retry import retry_transaction
from execution.scope import current_attempt

_tracking = ContextVar('progress_write', default=False)


class ProgressStore:
    @retry_transaction(profile='control')
    async def begin(self, ref, identity, stage, limit):
        async with transaction(profile='control') as conn:
            row = await store.assert_active(conn, ref.task, ref.token)
            remaining = limit.remaining()
            if remaining <= 0: raise ExecutionRevoked('Phase deadline already expired')
            until = min(row['execution_deadline_at'], row['observed_at'] + timedelta(seconds=remaining))
            inserted = await (await conn.execute('''INSERT INTO execution.task_activities(task_id,kind,token,id,stage,deadline_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
                (ref.task.id, ref.task.kind, ref.token, identity, stage, until))).fetchone()
            if inserted:
                await conn.execute('UPDATE execution.tasks SET progress_at=clock_timestamp(),progress_seq=progress_seq+1 WHERE id=%s', (ref.task.id,))
            await store.assert_active(conn, ref.task, ref.token)

    @retry_transaction(profile='control')
    async def waiting(self, ref, identity, reason):
        if ref is None or identity is None: return
        async with transaction(profile='control') as conn:
            await store.assert_active(conn, ref.task, ref.token)
            await conn.execute("""UPDATE execution.task_activities SET waiting_reason=%s
                WHERE task_id=%s AND token=%s AND id=%s AND finished_at IS NULL
                  AND waiting_reason IS DISTINCT FROM %s""", (reason,ref.task.id,ref.token,identity,reason))
            await store.assert_active(conn, ref.task, ref.token)

    @retry_transaction(profile='control')
    async def finish(self, ref, identity, *, failed=False):
        async with transaction(profile='control') as conn:
            row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE', (ref.task.id, ref.task.kind))).fetchone()
            now = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
            if store.authority_refusal(ref.task, ref.token, row, now): return True
            activity = await (await conn.execute('SELECT * FROM execution.task_activities WHERE task_id=%s AND token=%s AND id=%s', (ref.task.id, ref.token, identity))).fetchone()
            if not activity or activity['finished_at']: return True
            if not failed and activity['deadline_at'] <= now: return False
            await conn.execute('UPDATE execution.task_activities SET finished_at=%s WHERE task_id=%s AND token=%s AND id=%s', (now, ref.task.id, ref.token, identity))
            await conn.execute('UPDATE execution.tasks SET progress_at=%s,progress_seq=progress_seq+1 WHERE id=%s', (now, ref.task.id))
            await store.assert_active(conn, ref.task, ref.token)
            return True

    @retry_transaction(profile='control')
    async def inspect(self, task, token):
        async with transaction(profile='control') as conn:
            row = await (await conn.execute('SELECT * FROM execution.tasks WHERE id=%s AND kind=%s FOR UPDATE', (task.id, task.kind))).fetchone()
            now = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
            if store.authority_refusal(task, token, row, now): return False
            activities = await (await conn.execute('''SELECT * FROM execution.task_activities
                WHERE task_id=%s AND token=%s AND finished_at IS NULL ORDER BY deadline_at''', (task.id, token))).fetchall()
            idle_since = row['progress_at'] or row['started_at']
            expired = any(a['deadline_at'] <= now for a in activities) or not activities and (now-idle_since).total_seconds() >= number('PROGRESS_IDLE_SECONDS', 60)
            warning = expired or (not activities and (now-idle_since).total_seconds() >= number('PROGRESS_IDLE_SECONDS',60)*.8) or any(now >= a['started_at'] + (a['deadline_at']-a['started_at']) * .8 for a in activities)
            if warning: await alert(conn, str(task.kind), str(task.id), 'EXECUTION_STALLED' if expired else 'PROGRESS_SLOW')
            else: await alert(conn, str(task.kind), str(task.id), 'PROGRESS_SLOW', resolved=True)
            if not expired: return False
            code = 'EXECUTION_STALLED'
            if task.kind == TaskKind.AGENT:
                from agent_service.storage.receipts import resumable
                if not await resumable(conn, task.id): code = 'INTERRUPTED_UNKNOWN'
            # Recheck time, lease and user control after recovery verification.
            result = await store.settle_failure(conn, task, token, code, reason='progress_timeout')
            if result.accepted and task.kind == TaskKind.AGENT:
                from agent_service.storage.runtime import interrupt_calls
                await interrupt_calls(conn, task.id)
            return result.accepted

    async def snapshot(self, conn, task_id):
        row = await (await conn.execute('''SELECT progress_at AS last_progress_at,progress_seq,lease_token,status,kind,
            clock_timestamp() AS observed_at FROM execution.tasks WHERE id=%s''', (task_id,))).fetchone()
        if not row: return None
        activities = await (await conn.execute('''SELECT stage,started_at,deadline_at,waiting_reason FROM execution.task_activities
            WHERE task_id=%s AND token=%s AND finished_at IS NULL ORDER BY deadline_at''', (task_id, row['lease_token']))).fetchall()
        waiting = next((a['waiting_reason'] for a in activities if a['waiting_reason']), None)
        if row['status'] == 'queued':
            service = 'rag' if row['kind'] == 'rag_document' else 'agent'
            latest = (await (await conn.execute(f'SELECT max(heartbeat_at) AS at FROM {service}.worker_instances')).fetchone())['at']
            bindings = ['rag:embedding'] if service == 'rag' else ['agent:agent-model']
            if row['kind'] == 'memory_job': bindings.append('agent:embedding')
            cooled = await (await conn.execute('''SELECT 1 FROM execution.flow_buckets b
                JOIN execution.downstream_bindings d ON b.scope='downstream' AND b.key=d.group_key
                WHERE d.service=ANY(%s) AND b.cooldown_until>%s LIMIT 1''', (bindings, row['observed_at']))).fetchone()
            waiting = 'worker_unavailable' if latest is None or (row['observed_at']-latest).total_seconds()>=90 else 'downstream_cooldown' if cooled else 'queued'
        draining = await (await conn.execute("""SELECT observed_at,state->'draining_tasks' ? %s AS draining
            FROM execution.worker_resources WHERE kind=%s AND observed_at>clock_timestamp()-interval '30 seconds'
            ORDER BY observed_at DESC LIMIT 1""", (str(task_id), 'agent_run' if row['kind']=='memory_job' else row['kind']))).fetchone()
        return {'last_progress_at': row['last_progress_at'], 'sequence': row['progress_seq'],
                'observed_at': row['observed_at'], 'activities': activities,
                'waiting_reason': waiting,
                'draining': draining['draining'] if draining else None, 'draining_observed_at': draining['observed_at'] if draining else None}


@contextmanager
def control_budget(ref):
    from execution.persistence import resources, bind, call_scope
    from execution.infra.types import OperationDeadline
    runtime=resources()
    when=OperationDeadline.after(3).when
    if ref.deadline: when=min(when,ref.deadline.when)
    if runtime._closing_deadline: when=min(when,runtime._closing_deadline.when)
    with bind(runtime,deadline=OperationDeadline(when),cleanup=not runtime.accepting), call_scope(None):
        yield


@asynccontextmanager
async def activity(stage, limit, *, identity=None):
    ref = current_attempt()
    if ref is None or _tracking.get():
        yield None
        return
    identity = identity or uuid4()
    marker = _tracking.set(True)
    try: await ProgressStore().begin(ref, identity, stage, limit)
    finally: _tracking.reset(marker)
    failed = True
    timed_out = False
    try:
        yield identity
        failed = False
    except BaseException as exc:
        import psycopg
        timed_out = stage.startswith(('cpu:', 'database:')) and isinstance(exc, (TimeoutError, psycopg.errors.QueryCanceled)) and limit.remaining() <= 0
        if timed_out:
            marker = _tracking.set(True)
            try:
                with control_budget(ref): await ProgressStore().inspect(ref.task, ref.token)
            finally: _tracking.reset(marker)
            raise ExecutionRevoked('Work phase exceeded progress deadline') from exc
        raise
    finally:
        # Shutdown/control errors already revoke acceptance. Do not mask those
        # with a new progress write, and leave their unfinished activity visible.
        if not failed:
            marker = _tracking.set(True)
            try:
                with control_budget(ref):
                    if not await ProgressStore().finish(ref, identity):
                        await ProgressStore().inspect(ref.task, ref.token)
                        raise ExecutionRevoked('Phase exceeded progress deadline')
            finally: _tracking.reset(marker)
        elif not timed_out:
            from execution.persistence import resources
            if resources().accepting and not (ref.stopped and ref.stopped.is_set()):
                marker = _tracking.set(True)
                try:
                    from execution.errors import CONTROL_ERRORS
                    try:
                        with control_budget(ref): await ProgressStore().finish(ref, identity, failed=True)
                    except (*CONTROL_ERRORS, TimeoutError): pass
                finally: _tracking.reset(marker)
