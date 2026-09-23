from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from pydantic import Field

from contracts.errors import ApiError
from contracts.v1 import ErrorCode, StrictModel
from db.connection import connect
from identity.security import admin, new_key


router = APIRouter(prefix="/v1/admin", tags=["identity"], dependencies=[Depends(admin)])


class CreateTeam(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class IssueKey(StrictModel):
    expires_at: datetime | None = None


class SetExpiry(StrictModel):
    expires_at: datetime | None


@router.post("/teams", status_code=201)
def create_team(body: CreateTeam):
    team_id = uuid4()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        if conn.execute("SELECT 1 FROM identity.teams WHERE name = %s", (body.name,)).fetchone():
            raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "Team name already exists")
        conn.execute("INSERT INTO identity.teams(id, name) VALUES (%s, %s)", (team_id, body.name))
    return {"team_id": str(team_id), "name": body.name}


@router.post("/teams/{team_id}/keys", status_code=201)
def issue_key(team_id: UUID, body: IssueKey):
    if body.expires_at is not None and body.expires_at.tzinfo is None:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Expiry must include timezone")
    key_id, raw, digest = new_key()
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        if not conn.execute("SELECT 1 FROM identity.teams WHERE id = %s", (team_id,)).fetchone():
            raise ApiError(404, ErrorCode.NOT_FOUND, "Team not found")
        conn.execute("INSERT INTO identity.credentials(key_id, kind, team_id, digest, expires_at) VALUES (%s, 'team', %s, %s, %s)", (key_id, team_id, digest, body.expires_at))
    return {"key_id": key_id, "team_id": str(team_id), "key": raw, "expires_at": body.expires_at}


@router.put("/keys/{key_id}/expiry")
def set_expiry(key_id: str, body: SetExpiry):
    if body.expires_at is not None and body.expires_at.tzinfo is None:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Expiry must include timezone")
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        row = conn.execute("UPDATE identity.credentials SET expires_at = %s WHERE key_id = %s AND kind = 'team' RETURNING key_id", (body.expires_at, key_id)).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Key not found")
    return {"key_id": key_id, "expires_at": body.expires_at}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str):
    with connect("IDENTITY_ADMIN_DATABASE_URL") as conn:
        row = conn.execute("UPDATE identity.credentials SET revoked_at = COALESCE(revoked_at, now()) WHERE key_id = %s AND kind = 'team' RETURNING key_id, revoked_at", (key_id,)).fetchone()
        if not row:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Key not found")
    return {"key_id": key_id, "revoked_at": row["revoked_at"]}
