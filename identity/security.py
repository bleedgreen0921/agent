import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Header

from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from db.connection import connect


@dataclass(frozen=True)
class Principal:
    key_id: str
    kind: str
    team_id: UUID | None


def new_key() -> tuple[str, str, bytes]:
    key_id = "key_" + secrets.token_hex(12)
    secret = secrets.token_urlsafe(32)
    return key_id, f"{key_id}.{secret}", hashlib.sha256(secret.encode()).digest()


def verify(raw: str | None, kind: str, env_name: str) -> Principal:
    try:
        scheme, token = (raw or "").split(" ", 1)
        key_id, secret = token.split(".", 1)
        if scheme.lower() != "bearer" or not key_id.startswith("key_") or not secret:
            raise ValueError
    except ValueError:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Invalid credential") from None
    with connect(env_name) as conn:
        row = conn.execute("""SELECT key_id, kind, team_id, digest, expires_at, revoked_at
            FROM identity.credentials WHERE key_id = %s""", (key_id,)).fetchone()
        active_team = True
        if row and row["team_id"]:
            active_team = bool(conn.execute("SELECT 1 FROM identity.teams WHERE id = %s", (row["team_id"],)).fetchone())
    expected = bytes(row["digest"]) if row else bytes(32)
    matches = hmac.compare_digest(hashlib.sha256(secret.encode()).digest(), expected)
    if not row or not matches or row["kind"] != kind or row["revoked_at"] is not None or not active_team or (row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc)):
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Invalid credential")
    return Principal(row["key_id"], kind, row["team_id"])


def rag_team(authorization: str | None = Header(default=None)) -> Principal:
    return verify(authorization, "team", "RAG_DATABASE_URL")


def agent_team(authorization: str | None = Header(default=None)) -> Principal:
    return verify(authorization, "team", "AGENT_DATABASE_URL")


def admin(authorization: str | None = Header(default=None)) -> Principal:
    return verify(authorization, "admin", "RAG_DATABASE_URL")
