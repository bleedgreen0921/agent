"""Read-only personal Run operations."""
import base64
import binascii
import json
from datetime import datetime, timezone
from uuid import UUID
from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from execution.persistence import transaction as connect

def _invalid_cursor() -> ApiError:
    return ApiError(422, ErrorCode.INVALID_REQUEST, 'Invalid cursor')

def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Time filters must include a timezone')
    return value.astimezone(timezone.utc).isoformat()

def _decode_cursor(raw: str, filters: dict) -> tuple[datetime, UUID]:
    try:
        if len(raw) > 2048 or not raw or '=' in raw:
            raise ValueError
        data = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
        if base64.urlsafe_b64encode(data).decode().rstrip('=') != raw:
            raise ValueError
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {'v', 'filters', 'created_at', 'id'}:
            raise ValueError
        if type(value['v']) is not int or value['v'] != 1 or value['filters'] != filters:
            raise ValueError
        created_at = datetime.fromisoformat(value['created_at'])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        if created_at.astimezone(timezone.utc).isoformat() != value['created_at']:
            raise ValueError
        run_id = UUID(value['id'])
        if str(run_id) != value['id']:
            raise ValueError
        return (created_at, run_id)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError, binascii.Error, RecursionError):
        raise _invalid_cursor() from None

def _encode_cursor(row: dict, filters: dict) -> str:
    value = {'v': 1, 'filters': filters, 'created_at': _utc(row['created_at']), 'id': str(row['id'])}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')

async def list_runs(*, status: str | None, created_after: datetime | None, created_before: datetime | None, limit: int, cursor: str | None) -> dict:
    after, before = (_utc(created_after), _utc(created_before))
    filters = {'status': status, 'created_after': after, 'created_before': before}
    position = _decode_cursor(cursor, filters) if cursor is not None else None
    conditions = []
    params = []
    if status is not None:
        conditions.append('status=%s')
        params.append(status)
    if after is not None:
        conditions.append('created_at>%s')
        params.append(created_after)
    if before is not None:
        conditions.append('created_at<%s')
        params.append(created_before)
    if position is not None:
        conditions.append('(created_at,id)<(%s,%s)')
        params.extend(position)
    columns = 'id,mode,status,created_at,started_at,finished_at,error_code'
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    query = f'SELECT {columns} FROM agent.run_state{where} ORDER BY created_at DESC,id DESC LIMIT %s'
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        rows = await (await conn.execute(query, (*params, limit + 1))).fetchall()
    items = rows[:limit]
    return {'items': [dict(row) for row in items], 'next_cursor': _encode_cursor(items[-1], filters) if len(rows) > limit else None}

async def run_queue_summary() -> dict:
    query = """WITH clock AS (SELECT %s::timestamptz AS at)
        SELECT
          count(*) FILTER (WHERE status='queued' AND queue_deadline_at>clock.at) AS queued_within_deadline,
          count(*) FILTER (WHERE status='queued' AND queue_deadline_at<=clock.at) AS queued_past_deadline,
          count(*) FILTER (WHERE status='running' AND leased_until>clock.at) AS running_lease_valid,
          count(*) FILTER (WHERE status='running' AND leased_until<=clock.at) AS running_lease_expired,
          count(*) FILTER (WHERE status='cancelling') AS cancelling
        FROM agent.run_state CROSS JOIN clock"""
    async with connect('AGENT_DATABASE_URL', readonly=True) as conn:
        observed = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
        result=dict(await (await conn.execute(query, (observed,))).fetchone())
        from agent_service.storage.receipts import resumable
        queued=await (await conn.execute("SELECT min(created_at) AS at FROM agent.run_state WHERE status='queued' AND queue_deadline_at>%s AND recovery_protocol=2", (observed,))).fetchone()
        expired=await (await conn.execute("SELECT id,created_at FROM agent.run_state WHERE status='running' AND leased_until<=%s AND execution_deadline_at>%s AND stop_requested_at IS NULL", (observed, observed))).fetchall()
        eligible=[row['created_at'] for row in expired if await resumable(conn,row['id'])]
        if queued['at'] is not None:eligible.append(queued['at'])
        result['oldest_claimable_created_at']=min(eligible) if eligible else None
        return result
