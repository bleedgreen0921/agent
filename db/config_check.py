"""Local, role-specific configuration checks; no network access or secret output."""

import argparse
import math
import os
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict

from db.connection import TIMEOUT_PROFILES, positive_int_env


ROLES = ("agent-api", "agent-worker", "rag-api", "rag-worker")


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def database(name: str) -> None:
    dsn = required(name)
    try:
        conninfo_to_dict(dsn)
    except Exception as exc:
        raise ValueError(f"{name} must be a valid PostgreSQL DSN") from exc


def url(name: str, *, optional: bool = False) -> None:
    value = os.environ.get(name, "").strip()
    if not value and optional:
        return
    if not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        if valid:
            parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be an HTTP(S) URL with a host")


def seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def positive_int(name: str, default: int) -> int:
    try:
        return positive_int_env(name, default)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError("Unknown process role")
    positive_int("DB_CONNECT_TIMEOUT_SECONDS", 10)
    for statement_name, statement_default, lock_name, lock_default in TIMEOUT_PROFILES.values():
        for name, default in ((statement_name, statement_default), (lock_name, lock_default)):
            if positive_int(name, default) > 2_147_483:
                raise ValueError(f"{name} exceeds the PostgreSQL timeout limit")

    if role.startswith("agent"):
        database("AGENT_DATABASE_URL")
        positive_int("AGENT_QUEUE_TIMEOUT_SECONDS", 60)
    else:
        database("RAG_DATABASE_URL")

    if role == "agent-worker":
        url("AGENT_MODEL_URL")
        required("AGENT_MODEL")
        from agent_service.tooling import load_providers
        providers = load_providers()
        if any(provider.name == "rag_evidence" for _, provider in providers):
            url("RAG_BASE_URL")
            if len(required("RAG_SERVICE_TOKEN")) < 43:
                raise ValueError("RAG_SERVICE_TOKEN must contain at least 43 characters")
        positive_int("AGENT_MAX_CONCURRENT_RUNS", 2)
        lease = positive_int("AGENT_LEASE_SECONDS", 120)
        heartbeat = seconds("AGENT_HEARTBEAT_SECONDS", 30)
        if heartbeat * 3 > lease:
            raise ValueError("AGENT_HEARTBEAT_SECONDS must be no greater than one third of AGENT_LEASE_SECONDS")
        positive_int("AGENT_EXECUTION_TIMEOUT_SECONDS", 300)
        seconds("AGENT_CONTROL_POLL_SECONDS", 0.5)
        seconds("AGENT_TERMINATE_GRACE_SECONDS", 3)
        seconds("AGENT_MODEL_TIMEOUT_SECONDS", 120)
        seconds("AGENT_RAG_TIMEOUT_SECONDS", 120)
        seconds("AGENT_MEMORY_POLL_SECONDS", 1)
        seconds("AGENT_MEMORY_EMBED_TIMEOUT_SECONDS", 15)
        url("AGENT_EMBEDDING_URL", optional=True)
        url("RAG_EMBEDDING_URL", optional=True)
    elif role == "rag-api":
        database("IDENTITY_ADMIN_DATABASE_URL")
        if len(required("RAG_SERVICE_TOKEN")) < 43:
            raise ValueError("RAG_SERVICE_TOKEN must contain at least 43 characters")
        positive_int("RAG_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
        seconds("RAG_MODEL_TIMEOUT_SECONDS", 30)
        url("RAG_REWRITE_URL", optional=True)
        url("RAG_RERANK_URL", optional=True)
    elif role == "rag-worker":
        url("RAG_EMBEDDING_URL")
        seconds("RAG_MODEL_TIMEOUT_SECONDS", 30)
        positive_int("RAG_MAX_CHUNKS", 2000)
        positive_int("RAG_DOCUMENT_TIMEOUT_SECONDS", 14400)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate local process configuration")
    parser.add_argument("--role", required=True, choices=ROLES)
    args = parser.parse_args(argv)
    try:
        validate_role(args.role)
    except ValueError as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
    print(f"{args.role}: configuration valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
