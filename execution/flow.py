"""Short database transactions for shared request pressure; never hold locks over I/O."""
import asyncio
import logging
import math
import random
from dataclasses import dataclass
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

from execution.flow_config import downstream_policy, policy_hash
from execution.persistence import transaction, resources
from execution.retry import retry_transaction

log = logging.getLogger(__name__)


class WaitDelay(float):
    def __new__(cls, value, reason):
        result = super().__new__(cls, value)
        result.reason = reason
        return result


class DownstreamLimited(Exception):
    def __init__(self, retry_after=1, *, code='DOWNSTREAM_RATE_LIMITED'):
        self.retry_after = max(1, math.ceil(retry_after))
        self.code = code
        super().__init__(code)


async def bucket(conn, scope, key, policy):
    await conn.execute('''INSERT INTO execution.flow_buckets(scope,key,policy_hash,tokens)
        VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING''', (scope, key, policy_hash(policy), policy['burst']))
    row = await (await conn.execute('SELECT * FROM execution.flow_buckets WHERE scope=%s AND key=%s FOR UPDATE', (scope, key))).fetchone()
    if row['policy_hash'] != policy_hash(policy):
        raise RuntimeError('Shared flow policy differs; update all processes and reset policy as owner')
    now = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
    return row, now


async def alert(conn, scope, key, code, *, resolved=False):
    if resolved:
        await conn.execute('UPDATE execution.flow_alerts SET resolved_at=clock_timestamp() WHERE scope=%s AND key=%s AND code=%s AND resolved_at IS NULL', (scope, key, code))
        return
    prior = await (await conn.execute('SELECT resolved_at FROM execution.flow_alerts WHERE scope=%s AND key=%s AND code=%s', (scope,key,code))).fetchone()
    await conn.execute("""INSERT INTO execution.flow_alerts(scope,key,code) VALUES (%s,%s,%s)
        ON CONFLICT(scope,key,code) DO UPDATE SET last_seen_at=clock_timestamp(),resolved_at=NULL""", (scope,key,code))
    row = prior is None or prior['resolved_at'] is not None
    if row:
        log.warning('Flow control alert', extra={'scope': scope, 'subject': key, 'code': code})


def refill(row, now, policy):
    rate = policy['rate'] * row['rate_factor']
    return min(policy['burst'], row['tokens'] + max(0, (now - row['sampled_at']).total_seconds()) * rate), rate


@dataclass(frozen=True)
class Permit:
    key: str
    generation: int
    id: object


def retry_seconds(value, now):
    if value is None: return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed - now).total_seconds()
        except (ValueError, TypeError, OverflowError): return None
    if not math.isfinite(seconds) or seconds < 0: return None
    # datetime overflow is invalid input, not a reason to send earlier.
    try: now + timedelta(seconds=seconds)
    except OverflowError: return None
    return max(1, seconds)


class DownstreamGovernor:
    @retry_transaction(profile='control')
    async def register(self, service, url):
        key, policy = downstream_policy(service, url)
        binding = resources().role.split('-')[0] + ':' + service
        async with transaction(profile='control') as conn:
            await bucket(conn, 'downstream', key, policy)
            await conn.execute('INSERT INTO execution.downstream_bindings(service,group_key) VALUES (%s,%s) ON CONFLICT(service) DO NOTHING', (binding, key))
            binding_row = await (await conn.execute('SELECT group_key FROM execution.downstream_bindings WHERE service=%s', (binding,))).fetchone()
            if binding_row['group_key'] != key:
                raise RuntimeError('Configured downstream changed; configure flow policies as owner before starting')
        return key, policy

    @retry_transaction(profile='control')
    async def acquire(self, service, url, seconds):
        key, policy = downstream_policy(service, url)
        identity = uuid4()
        binding = resources().role.split('-')[0] + ':' + service
        async with transaction(profile='control') as conn:
            row, now = await bucket(conn, 'downstream', key, policy)
            await conn.execute('INSERT INTO execution.downstream_bindings(service,group_key) VALUES (%s,%s) ON CONFLICT(service) DO NOTHING', (binding, key))
            if row['cooldown_until'] and row['cooldown_until'] > now:
                return None, WaitDelay((row['cooldown_until'] - now).total_seconds(), 'downstream_cooldown')
            if row['unhealthy_since'] and row['probe_until'] and row['probe_until'] > now:
                return None, WaitDelay(max(.05, (row['probe_until'] - now).total_seconds()), 'downstream_probe')
            tokens, rate = refill(row, now, policy)
            if tokens < 1:
                return None, WaitDelay((1 - tokens) / rate, 'downstream_rate')
            probe = row['unhealthy_since'] is not None
            await conn.execute('''UPDATE execution.flow_buckets SET tokens=%s,sampled_at=%s,
                probe_id=%s,probe_until=%s WHERE scope='downstream' AND key=%s''',
                (tokens - 1, now, identity if probe else None,
                 now + timedelta(seconds=max(.001, seconds) + 5) if probe else None, key))
            return Permit(key, row['generation'], identity), 0

    @retry_transaction(profile='control')
    async def feedback(self, permit, *, status=None, retry_after=None, transport_error=False):
        async with transaction(profile='control') as conn:
            row = await (await conn.execute("SELECT * FROM execution.flow_buckets WHERE scope='downstream' AND key=%s FOR UPDATE", (permit.key,))).fetchone()
            now = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
            limited = status == 429
            if not row or row['generation'] != permit.generation and not limited:
                return
            failed = transport_error or status is not None and (status >= 500 or status in (401, 403))
            if limited or failed:
                failures = row['failures'] + 1
                cool = limited or failures >= 3 or row['unhealthy_since'] is not None or status in (401, 403)
                code = 'DOWNSTREAM_RATE_LIMITED' if limited else 'DOWNSTREAM_AUTH_FAILED' if status in (401, 403) else 'DOWNSTREAM_UNAVAILABLE'
                delay = retry_seconds(retry_after, now)
                if delay is None: delay = min(60, 5 * 2 ** min(failures - 1, 4)) * random.uniform(1, 1.2)
                await conn.execute('''UPDATE execution.flow_buckets SET failures=%s,successes=0,
                    rate_factor=%s,cooldown_until=%s,unhealthy_since=CASE WHEN %s THEN COALESCE(unhealthy_since,%s) ELSE unhealthy_since END,
                    generation=generation+%s,probe_id=NULL,probe_until=NULL,last_error=%s
                    WHERE scope='downstream' AND key=%s''', (failures, max(.125, row['rate_factor'] / 2) if limited else row['rate_factor'],
                    max(now + timedelta(seconds=delay), row['cooldown_until'] or now) if cool else None,
                    cool, now, 1 if cool else 0, code, permit.key))
                if cool: await alert(conn, 'downstream', permit.key, 'DOWNSTREAM_UNAVAILABLE')
            elif status is not None and status < 400:
                if row['unhealthy_since'] and row['probe_id'] != permit.id:
                    return
                successes = row['successes'] + 1
                factor = min(1., row['rate_factor'] + .1) if successes >= 10 else row['rate_factor']
                await conn.execute('''UPDATE execution.flow_buckets SET failures=0,successes=%s,rate_factor=%s,
                    cooldown_until=NULL,unhealthy_since=NULL,probe_id=NULL,probe_until=NULL,last_error=NULL
                    WHERE scope='downstream' AND key=%s''', (0 if successes >= 10 else successes, factor, permit.key))
                await alert(conn, 'downstream', permit.key, 'DOWNSTREAM_UNAVAILABLE', resolved=True)
            elif row['probe_id'] == permit.id:
                # A non-transient provider rejection proves transport availability.
                await conn.execute("""UPDATE execution.flow_buckets SET probe_id=NULL,probe_until=NULL,
                    unhealthy_since=NULL,cooldown_until=NULL,failures=0,last_error=NULL
                    WHERE scope='downstream' AND key=%s""", (permit.key,))
                await alert(conn, 'downstream', permit.key, 'DOWNSTREAM_UNAVAILABLE', resolved=True)


async def pressure_snapshot(conn):
    return await (await conn.execute('''SELECT key AS group_id,cooldown_until,unhealthy_since,
        rate_factor,last_error,probe_until FROM execution.flow_buckets WHERE scope='downstream' ORDER BY key''')).fetchall()
