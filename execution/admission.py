"""Atomic admission. Count existing work; reserve future Memory work at turn acceptance."""
from contextlib import asynccontextmanager
from datetime import timedelta
import math
from uuid import uuid4
from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from execution.flow_config import admission_policy, number
from execution.flow import bucket, refill, alert
from execution.persistence import transaction, resources
from execution.retry import retry_transaction


def rejected(code, message, delay=5, status=429):
    return ApiError(status, code, message, headers={'Retry-After': str(max(1, math.ceil(delay)))})


async def memory_lock(conn):
    await conn.execute('SELECT pg_advisory_xact_lock(824731102)')
    await bucket(conn, 'memory_job', 'capacity', {'rate':1,'burst':number('MEMORY_UNFINISHED_CAPACITY',32,integer=True)})


async def memory_load(conn):
    return (await (await conn.execute("""SELECT
        (SELECT count(*) FROM execution.tasks WHERE kind='memory_job' AND status IN ('queued','running','stopping')) +
        COALESCE((SELECT sum(slots) FROM execution.capacity_reservations),0) AS n""")).fetchone())['n']


async def reserve_memory(conn, run_id):
    await memory_lock(conn)
    if await memory_load(conn) + 2 > number('MEMORY_UNFINISHED_CAPACITY', 32, integer=True):
        raise rejected(ErrorCode.QUEUE_CAPACITY_EXCEEDED, 'Memory capacity is full')
    await conn.execute('INSERT INTO execution.capacity_reservations(run_id,slots) VALUES (%s,2)', (run_id,))


async def release_memory(conn, task):
    if task.kind == 'agent_run':
        await conn.execute('DELETE FROM execution.capacity_reservations WHERE run_id=%s', (task.id,))


async def availability(conn, kind, row, now, *, reserve_probe=True):
    if kind == 'search': return None
    service = 'agent' if kind == 'agent_run' else 'rag'
    latest = (await (await conn.execute(f'SELECT max(heartbeat_at) AS at FROM {service}.worker_instances')).fetchone())['at']
    online = latest is not None and (now - latest).total_seconds() < 90
    binding = 'agent:agent-model' if kind == 'agent_run' else 'rag:embedding'
    failures = await (await conn.execute('''SELECT b.* FROM execution.flow_buckets b
        JOIN execution.downstream_bindings d ON b.scope='downstream' AND b.key=d.group_key
        WHERE d.service=%s AND b.unhealthy_since IS NOT NULL''', (binding,))).fetchall()
    unavailable = not online or bool(failures)
    sources = [row['unhealthy_since'] or now, *[r['unhealthy_since'] for r in failures]]
    if not online and latest is not None: sources.append(latest + timedelta(seconds=90))
    since = min(sources)
    await conn.execute('UPDATE execution.flow_buckets SET unhealthy_since=%s,probe_until=CASE WHEN %s THEN probe_until ELSE NULL END WHERE scope=%s AND key=%s',
                       (since if unavailable else None, unavailable, kind, 'submit'))
    grace = number('FLOW_UNAVAILABLE_GRACE_SECONDS', 30)
    if unavailable and (now - since).total_seconds() >= grace:
        await alert(conn, kind, 'submit', 'SERVICE_UNAVAILABLE')
    else:
        await alert(conn, kind, 'submit', 'SERVICE_UNAVAILABLE', resolved=True)
    if not unavailable or (now - since).total_seconds() < grace: return None
    if not online: return rejected(ErrorCode.SERVICE_UNAVAILABLE, 'No available Worker', status=503)
    until = max((r['cooldown_until'] for r in failures if r['cooldown_until']), default=now)
    if until > now:
        return rejected(ErrorCode.SERVICE_UNAVAILABLE, 'Required downstream is cooling down', (until - now).total_seconds(), 503)
    # A single new task can provide the next real recovery observation.
    if row['probe_until'] and row['probe_until'] > now:
        return rejected(ErrorCode.SERVICE_UNAVAILABLE, 'Waiting for downstream recovery probe', 5, 503)
    if reserve_probe:
        await conn.execute('UPDATE execution.flow_buckets SET probe_until=%s WHERE scope=%s AND key=%s',
                           (now + timedelta(seconds=60), kind, 'submit'))
    return None


class AdmissionStore:
    async def accept_task(self, conn, task, *, reservation_run_id=None):
        kind = str(task.kind)
        if kind == 'memory_job':
            await memory_lock(conn)
            if reservation_run_id:
                row = await (await conn.execute('SELECT slots FROM execution.capacity_reservations WHERE run_id=%s FOR UPDATE', (reservation_run_id,))).fetchone()
                if row:
                    if row['slots'] == 1:
                        await conn.execute('DELETE FROM execution.capacity_reservations WHERE run_id=%s', (reservation_run_id,))
                    else:
                        await conn.execute('UPDATE execution.capacity_reservations SET slots=slots-1 WHERE run_id=%s', (reservation_run_id,))
                    return
            if await memory_load(conn) >= number('MEMORY_UNFINISHED_CAPACITY', 32, integer=True):
                raise rejected(ErrorCode.QUEUE_CAPACITY_EXCEEDED, 'Memory capacity is full')
            return
        policy = admission_policy(kind)
        row, now = await bucket(conn, kind, 'submit', policy)
        unavailable = await availability(conn, kind, row, now)
        if unavailable: raise unavailable
        counts = await (await conn.execute("""SELECT count(*) AS unfinished,
            count(*) FILTER(WHERE status='queued') AS queued FROM execution.tasks
            WHERE kind=%s AND status IN ('queued','running','stopping')""", (kind,))).fetchone()
        if counts['unfinished'] >= policy['unfinished'] or counts['queued'] >= policy['queued']:
            raise rejected(ErrorCode.QUEUE_CAPACITY_EXCEEDED, 'Task queue capacity is full')
        if counts['unfinished'] + 1 >= .8 * policy['unfinished']:
            await alert(conn, kind, 'submit', 'QUEUE_NEAR_CAPACITY')
        else: await alert(conn, kind, 'submit', 'QUEUE_NEAR_CAPACITY', resolved=True)
        tokens, rate = refill(row, now, policy)
        if tokens < 1: raise rejected(ErrorCode.SUBMISSION_RATE_LIMITED, 'Task submission rate exceeded', (1 - tokens) / rate)
        await conn.execute('UPDATE execution.flow_buckets SET tokens=%s,sampled_at=%s WHERE scope=%s AND key=%s',
                           (tokens - 1, now, kind, 'submit'))

    @retry_transaction(profile='control')
    async def _request(self, scope, identity, seconds):
        policy = admission_policy('search') if scope == 'search' else {'rate': 2, 'burst': 2, 'unfinished': 2}
        async with transaction(profile='control') as conn:
            row, now = await bucket(conn, scope, 'request', policy)
            await conn.execute('DELETE FROM execution.request_permits WHERE scope=%s AND expires_at<=%s', (scope, now))
            count = (await (await conn.execute('SELECT count(*) AS n FROM execution.request_permits WHERE scope=%s', (scope,))).fetchone())['n']
            if count >= policy['unfinished']: raise rejected(ErrorCode.QUEUE_CAPACITY_EXCEEDED, 'Request capacity is full')
            tokens, rate = refill(row, now, policy)
            if scope != 'upload':
                if tokens < 1: raise rejected(ErrorCode.SUBMISSION_RATE_LIMITED, 'Request rate exceeded', (1-tokens)/rate)
                await conn.execute('UPDATE execution.flow_buckets SET tokens=%s,sampled_at=%s WHERE scope=%s AND key=%s', (tokens-1, now, scope, 'request'))
            await conn.execute('INSERT INTO execution.request_permits(id,scope,expires_at) VALUES (%s,%s,%s)',
                               (identity, scope, now + timedelta(seconds=seconds + 5)))

    @asynccontextmanager
    async def request(self, scope, deadline):
        identity = uuid4()
        await self._request(scope, identity, deadline.remaining())
        try: yield
        finally:
            async def release():
                from execution.persistence import bind
                runtime = resources()
                from execution.infra.types import OperationDeadline
                limit = runtime._closing_deadline or OperationDeadline.after(3)
                with bind(runtime, deadline=limit, cleanup=not runtime.accepting):
                    async with transaction(profile='control') as conn:
                        await conn.execute('DELETE FROM execution.request_permits WHERE id=%s AND scope=%s', (identity, scope))
            await resources().cleanup(release())

    @retry_transaction(profile='control')
    async def status(self, kind):
        policy = admission_policy(kind)
        async with transaction(profile='control') as conn:
            row, now = await bucket(conn, kind, 'request' if kind == 'search' else 'submit', policy)
            error = await availability(conn, kind, row, now, reserve_probe=False)
            counts = await (await conn.execute("""SELECT count(*) AS unfinished,
                count(*) FILTER(WHERE status='queued') AS queued FROM execution.tasks
                WHERE kind=%s AND status IN ('queued','running','stopping')""", (kind,))).fetchone()
            if kind == 'search':
                counts = {'unfinished': (await (await conn.execute("SELECT count(*) AS n FROM execution.request_permits WHERE scope='search' AND expires_at>%s", (now,))).fetchone())['n'], 'queued': 0}
            full = counts['unfinished'] >= policy['unfinished'] or counts['queued'] >= policy['queued']
            tokens, rate = refill(row, now, policy)
            limited = tokens < 1
            return {'accepting': not error and not full and not limited, 'reason': str(error.code) if error else 'QUEUE_CAPACITY_EXCEEDED' if full else 'SUBMISSION_RATE_LIMITED' if limited else None,
                    'counts': counts, 'limits': policy, 'observed_at': now, 'retry_after_seconds': max(1, math.ceil((1-tokens)/rate)) if limited else 5 if full or error else None}

    async def capabilities(self, service):
        if service == 'rag':
            return {'documents': await self.status('rag_document'), 'search': await self.status('search')}
        state = await self.status('agent_run')
        async with transaction(profile='control', readonly=True) as conn:
            memory_full = await memory_load(conn) + 2 > number('MEMORY_UNFINISHED_CAPACITY',32,integer=True)
        return {'runs': state, 'turns': {**state,'accepting':state['accepting'] and not memory_full,
                'reason':'QUEUE_CAPACITY_EXCEEDED' if memory_full else state['reason']}}
