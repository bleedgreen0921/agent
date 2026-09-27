"""Read-only Run operations for team and platform views."""

import base64
import binascii
import json
from datetime import datetime, timezone
from uuid import UUID

from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from db.connection import connect


def _invalid_cursor() -> ApiError:
    return ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid cursor")


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Time filters must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _decode_cursor(raw: str, filters: dict) -> tuple[datetime, UUID]:
    try:
        if len(raw) > 2048 or not raw or "=" in raw:
            raise ValueError
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if base64.urlsafe_b64encode(data).decode().rstrip("=") != raw:
            raise ValueError
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"v", "filters", "created_at", "id"}:
            raise ValueError
        if type(value["v"]) is not int or value["v"] != 1 or value["filters"] != filters:
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        if created_at.astimezone(timezone.utc).isoformat() != value["created_at"]:
            raise ValueError
        run_id = UUID(value["id"])
        if str(run_id) != value["id"]:
            raise ValueError
        return created_at, run_id
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError,
            binascii.Error, RecursionError):
        raise _invalid_cursor() from None


def _encode_cursor(row: dict, filters: dict) -> str:
    value = {"v": 1, "filters": filters, "created_at": _utc(row["created_at"]), "id": str(row["id"])}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")


def list_runs(*, team_id: UUID | None, admin: bool, status: str | None, created_after: datetime | None,
              created_before: datetime | None, limit: int, cursor: str | None) -> dict:
    after, before = _utc(created_after), _utc(created_before)
    filters = {"scope": "admin" if admin else "team", "team_id": str(team_id) if team_id else None,
               "status": status, "created_after": after, "created_before": before}
    position = _decode_cursor(cursor, filters) if cursor is not None else None
    conditions = []
    params = []
    if team_id is not None:
        conditions.append("team_id=%s")
        params.append(team_id)
    if status is not None:
        conditions.append("status=%s")
        params.append(status)
    if after is not None:
        conditions.append("created_at>%s")
        params.append(created_after)
    if before is not None:
        conditions.append("created_at<%s")
        params.append(created_before)
    if position is not None:
        conditions.append("(created_at,id)<(%s,%s)")
        params.extend(position)
    columns = "id,team_id,mode,status,created_at,started_at,finished_at,error_code" if admin else "id,mode,status,created_at,started_at,finished_at,error_code"
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    query = f"SELECT {columns} FROM agent.agent_runs{where} ORDER BY created_at DESC,id DESC LIMIT %s"
    with connect("AGENT_DATABASE_URL") as conn:
        conn.read_only = True
        rows = conn.execute(query, (*params, limit + 1)).fetchall()
    items = rows[:limit]
    return {"items": [dict(row) for row in items],
            "next_cursor": _encode_cursor(items[-1], filters) if len(rows) > limit else None}


def run_queue_summary() -> dict:
    query = """WITH clock AS (SELECT statement_timestamp() AS at)
        SELECT
          count(*) FILTER (WHERE status='queued' AND queue_deadline_at>clock.at) AS queued_within_deadline,
          count(*) FILTER (WHERE status='queued' AND queue_deadline_at<=clock.at) AS queued_past_deadline,
          count(*) FILTER (WHERE status='running' AND leased_until>clock.at) AS running_lease_valid,
          count(*) FILTER (WHERE status='running' AND leased_until<=clock.at) AS running_lease_expired,
          count(*) FILTER (WHERE status='cancelling') AS cancelling,
          min(r.created_at) FILTER (WHERE (r.status='queued' AND r.queue_deadline_at>clock.at)
            OR (r.status='running' AND r.leased_until<=clock.at AND r.execution_deadline_at>clock.at
              AND NOT EXISTS (SELECT 1 FROM agent.model_calls m WHERE m.run_id=r.id
                AND (m.status='started' OR NOT m.checkpointed))
              AND NOT EXISTS (SELECT 1 FROM agent.tool_calls t WHERE t.run_id=r.id
                AND (t.status='started' OR NOT t.checkpointed))))
            AS oldest_claimable_created_at
        FROM agent.agent_runs r CROSS JOIN clock"""
    with connect("AGENT_DATABASE_URL") as conn:
        conn.read_only = True
        return dict(conn.execute(query).fetchone())
