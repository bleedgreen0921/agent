"""Read-only deployment diagnostics for the Research Agent Platform."""

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.rows import dict_row

from db.migrate import ROOT


SCHEMA_VERSION = 1
REQUIRED_ENV = (
    "MIGRATION_DATABASE_URL",
    "RAG_DATABASE_URL",
    "AGENT_DATABASE_URL",
    "IDENTITY_ADMIN_DATABASE_URL",
)
ENV_LABELS = {
    "MIGRATION_DATABASE_URL": "owner",
    "RAG_DATABASE_URL": "rag_runtime",
    "AGENT_DATABASE_URL": "agent_runtime",
    "IDENTITY_ADMIN_DATABASE_URL": "identity_admin",
}
SAMPLE_LIMIT = 20


def result(code: str, status: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "status": status, "message": message, "details": details or {}}


@contextmanager
def readonly_connection(env_name: str):
    dsn = os.environ[env_name]
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.read_only = True
        yield conn


def _safe_check(code: str, function: Callable[[], dict]) -> dict:
    try:
        return function()
    except KeyError:
        return result(code, "error", "Required database configuration is missing")
    except (psycopg.Error, OSError, ValueError):
        return result(code, "error", "The check could not be completed")


def check_required_env() -> dict:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        return result("config.required_env", "error", "Required database configuration is missing", {"missing": missing})
    return result("config.required_env", "pass", "All required database connection settings are present")


def check_connectivity() -> dict:
    failed = []
    connected = []
    for env_name in REQUIRED_ENV:
        if not os.environ.get(env_name):
            failed.append(ENV_LABELS[env_name])
            continue
        try:
            with readonly_connection(env_name) as conn:
                conn.execute("SELECT 1").fetchone()
            connected.append(ENV_LABELS[env_name])
        except psycopg.Error:
            failed.append(ENV_LABELS[env_name])
    if failed:
        return result("db.connectivity", "error", "One or more database roles could not connect", {"failed_roles": failed, "connected_roles": connected})
    return result("db.connectivity", "pass", "Owner and runtime database roles can connect", {"roles": connected})


def _migration_heads() -> dict[str, str]:
    heads = {}
    locations = {"identity": "identity", "rag": "rag_service", "agent": "agent_service"}
    for schema, package in locations.items():
        config = Config()
        config.set_main_option("script_location", str(ROOT / package / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        if head is None:
            raise ValueError("migration chain has no head")
        heads[schema] = head
    return heads


def check_migration_heads() -> dict:
    expected = _migration_heads()
    actual = {}
    with readonly_connection("MIGRATION_DATABASE_URL") as conn:
        for schema in expected:
            try:
                row = conn.execute(sql.SQL("SELECT version_num FROM {}.alembic_version").format(sql.Identifier(schema))).fetchone()
                actual[schema] = row["version_num"] if row else None
            except psycopg.errors.UndefinedTable:
                conn.rollback()
                conn.read_only = True
                actual[schema] = None
    behind = {schema: {"expected": expected[schema], "actual": actual[schema]} for schema in expected if actual[schema] != expected[schema]}
    if behind:
        return result("migrations.heads", "error", "One or more schema migration chains are not at head", {"mismatches": behind})
    return result("migrations.heads", "pass", "All schema migration chains are at head", {"heads": actual})


def check_pgvector() -> dict:
    with readonly_connection("MIGRATION_DATABASE_URL") as conn:
        row = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
    if not row:
        return result("db.pgvector", "error", "The vector extension is not installed")
    return result("db.pgvector", "pass", "The vector extension is installed", {"version": row["extversion"]})


def check_checkpoints() -> dict:
    required = ["checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"]
    with readonly_connection("MIGRATION_DATABASE_URL") as conn:
        rows = conn.execute("""SELECT tablename FROM pg_tables
            WHERE schemaname='agent' AND tablename=ANY(%s)""", (required,)).fetchall()
        present = {row["tablename"] for row in rows}
        initialized = 0
        if "checkpoint_migrations" in present:
            initialized = conn.execute("SELECT count(*) AS n FROM agent.checkpoint_migrations").fetchone()["n"]
    missing = sorted(set(required) - present)
    if missing or not initialized:
        return result("agent.checkpoints", "error", "LangGraph checkpoint tables are missing or uninitialized", {"missing": missing, "migration_rows": initialized})
    return result("agent.checkpoints", "pass", "LangGraph checkpoint tables are present and initialized", {"migration_rows": initialized})


def check_role_permissions() -> dict:
    roles = ("rag_runtime", "agent_runtime", "identity_admin")
    schemas = ("identity", "rag", "agent")
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE")
    violations: list[dict] = []
    with readonly_connection("MIGRATION_DATABASE_URL") as conn:
        existing_roles = {row["rolname"] for row in conn.execute("SELECT rolname FROM pg_roles WHERE rolname=ANY(%s)", (list(roles),)).fetchall()}
        for role in roles:
            if role not in existing_roles:
                violations.append({"role": role, "object": "role", "issue": "missing"})
                continue
            database_access = conn.execute("SELECT has_database_privilege(%s,current_database(),'CONNECT') AS allowed", (role,)).fetchone()["allowed"]
            if not database_access:
                violations.append({"role": role, "object": "database", "privilege": "CONNECT", "expected": True, "actual": False})
            public_usage = conn.execute("SELECT has_schema_privilege(%s,'public','USAGE') AS allowed", (role,)).fetchone()["allowed"]
            if public_usage != (role == "rag_runtime"):
                violations.append({"role": role, "object": "public", "privilege": "USAGE", "expected": role == "rag_runtime", "actual": public_usage})
            for schema in schemas:
                expected_usage = schema == {"rag_runtime": "rag", "agent_runtime": "agent", "identity_admin": "identity"}[role] or (schema == "identity" and role in {"rag_runtime", "agent_runtime"})
                actual_usage = conn.execute("SELECT has_schema_privilege(%s,%s,'USAGE') AS allowed", (role, schema)).fetchone()["allowed"]
                if actual_usage != expected_usage:
                    violations.append({"role": role, "object": schema, "privilege": "USAGE", "expected": expected_usage, "actual": actual_usage})
                actual_create = conn.execute("SELECT has_schema_privilege(%s,%s,'CREATE') AS allowed", (role, schema)).fetchone()["allowed"]
                if actual_create:
                    violations.append({"role": role, "object": schema, "privilege": "CREATE", "expected": False, "actual": True})
            tables = conn.execute("""SELECT schemaname,tablename FROM pg_tables
                WHERE schemaname=ANY(%s) ORDER BY schemaname,tablename""", (list(schemas),)).fetchall()
            for table in tables:
                schema, name = table["schemaname"], table["tablename"]
                qualified = f"{schema}.{name}"
                if name == "alembic_version":
                    expected = set()
                elif role == "rag_runtime" and schema == "rag":
                    expected = set(privileges)
                elif role == "agent_runtime" and schema == "agent":
                    expected = {"SELECT", "INSERT"} if name == "run_manifests" else set(privileges)
                elif role == "identity_admin" and schema == "identity":
                    expected = set(privileges)
                elif role in {"rag_runtime", "agent_runtime"} and schema == "identity" and name in {"teams", "credentials"}:
                    expected = {"SELECT"}
                else:
                    expected = set()
                for privilege in privileges:
                    actual = conn.execute("SELECT has_table_privilege(%s,%s,%s) AS allowed", (role, qualified, privilege)).fetchone()["allowed"]
                    if actual != (privilege in expected):
                        violations.append({"role": role, "object": qualified, "privilege": privilege, "expected": privilege in expected, "actual": actual})
    if violations:
        return result("db.role_permissions", "error", "Database role permissions do not match the isolation matrix", {"count": len(violations), "samples": violations[:SAMPLE_LIMIT]})
    return result("db.role_permissions", "pass", "Database role permissions match the isolation matrix")


def check_active_revision() -> dict:
    with readonly_connection("RAG_DATABASE_URL") as conn:
        active = conn.execute("SELECT id FROM rag.index_revisions WHERE state='active' ORDER BY id").fetchall()
        states = conn.execute("""SELECT s.active_revision_id,r.state FROM rag.index_state s
            LEFT JOIN rag.index_revisions r ON r.id=s.active_revision_id WHERE s.singleton=true""").fetchall()
    consistent = len(active) == 1 and len(states) == 1 and states[0]["state"] == "active" and states[0]["active_revision_id"] == active[0]["id"]
    if not consistent:
        return result("rag.active_revision", "error", "The active index revision and index_state are inconsistent", {"active_revision_count": len(active), "index_state_count": len(states)})
    return result("rag.active_revision", "pass", "The active index revision is unique and consistent", {"revision_id": str(active[0]["id"])})


def check_embedding_coverage() -> dict:
    with readonly_connection("RAG_DATABASE_URL") as conn:
        rows = conn.execute("""SELECT c.id FROM rag.index_state s
            JOIN rag.chunks c ON true
            JOIN rag.documents d ON d.id=c.document_id AND d.active_version_id=c.version_id
            LEFT JOIN rag.embeddings e ON e.chunk_id=c.id AND e.revision_id=s.active_revision_id
            WHERE s.singleton=true AND d.deleted_at IS NULL AND e.chunk_id IS NULL
            ORDER BY c.id LIMIT %s""", (SAMPLE_LIMIT + 1,)).fetchall()
        total = conn.execute("""SELECT count(*) AS n FROM rag.index_state s
            JOIN rag.chunks c ON true
            JOIN rag.documents d ON d.id=c.document_id AND d.active_version_id=c.version_id
            LEFT JOIN rag.embeddings e ON e.chunk_id=c.id AND e.revision_id=s.active_revision_id
            WHERE s.singleton=true AND d.deleted_at IS NULL AND e.chunk_id IS NULL""").fetchone()["n"]
    if total:
        return result("rag.embedding_coverage", "error", "Active document chunks are missing active-revision embeddings", {"count": total, "samples": [str(row["id"]) for row in rows[:SAMPLE_LIMIT]]})
    return result("rag.embedding_coverage", "pass", "All active document chunks have active-revision embeddings")


def check_processing_leases() -> dict:
    with readonly_connection("RAG_DATABASE_URL") as conn:
        total = conn.execute("SELECT count(*) AS n FROM rag.processing_jobs WHERE status='running' AND (leased_until IS NULL OR leased_until<=now())").fetchone()["n"]
        rows = conn.execute("SELECT id FROM rag.processing_jobs WHERE status='running' AND (leased_until IS NULL OR leased_until<=now()) ORDER BY id LIMIT %s", (SAMPLE_LIMIT,)).fetchall()
    if total:
        return result("rag.processing_leases", "warn", "Processing jobs have expired running leases", {"count": total, "samples": [str(row["id"]) for row in rows]})
    return result("rag.processing_leases", "pass", "No processing jobs have expired running leases")


def check_run_leases() -> dict:
    with readonly_connection("AGENT_DATABASE_URL") as conn:
        total = conn.execute("SELECT count(*) AS n FROM agent.agent_runs WHERE status IN ('running','cancelling') AND (leased_until IS NULL OR leased_until<=now())").fetchone()["n"]
        rows = conn.execute("SELECT id FROM agent.agent_runs WHERE status IN ('running','cancelling') AND (leased_until IS NULL OR leased_until<=now()) ORDER BY id LIMIT %s", (SAMPLE_LIMIT,)).fetchall()
    if total:
        return result("agent.run_leases", "warn", "Agent Runs have expired active leases", {"count": total, "samples": [str(row["id"]) for row in rows]})
    return result("agent.run_leases", "pass", "No active Agent Runs have expired leases")


def check_call_consistency() -> dict:
    with readonly_connection("AGENT_DATABASE_URL") as conn:
        terminal = conn.execute("""SELECT DISTINCT r.id FROM agent.agent_runs r
            WHERE r.status IN ('completed','partial','failed','cancelled') AND (
              EXISTS (SELECT 1 FROM agent.model_calls m WHERE m.run_id=r.id AND m.status='started') OR
              EXISTS (SELECT 1 FROM agent.tool_calls t WHERE t.run_id=r.id AND t.status='started'))
            ORDER BY r.id""").fetchall()
        unsafe = conn.execute("""SELECT DISTINCT r.id FROM agent.agent_runs r
            WHERE r.status IN ('running','cancelling') AND (r.leased_until IS NULL OR r.leased_until<=now()) AND (
              EXISTS (SELECT 1 FROM agent.model_calls m WHERE m.run_id=r.id AND (m.status='started' OR NOT m.checkpointed)) OR
              EXISTS (SELECT 1 FROM agent.tool_calls t WHERE t.run_id=r.id AND (t.status='started' OR NOT t.checkpointed)))
            ORDER BY r.id""").fetchall()
    if terminal:
        return result("agent.call_consistency", "error", "Terminal Runs still contain started calls", {
            "terminal_started_count": len(terminal),
            "terminal_started_samples": [str(row["id"]) for row in terminal[:SAMPLE_LIMIT]],
            "lost_unsafe_count": len(unsafe),
            "lost_unsafe_samples": [str(row["id"]) for row in unsafe[:SAMPLE_LIMIT]],
        })
    if unsafe:
        return result("agent.call_consistency", "warn", "Expired active Runs contain calls that are unsafe to resume", {"count": len(unsafe), "samples": [str(row["id"]) for row in unsafe[:SAMPLE_LIMIT]]})
    return result("agent.call_consistency", "pass", "Agent call state is consistent with Run state")


def check_files_consistency() -> dict:
    root = Path(os.environ.get("RAG_FILES_DIR", ".data/rag")).resolve()
    with readonly_connection("RAG_DATABASE_URL") as conn:
        rows = conn.execute("SELECT id,file_path FROM rag.document_versions ORDER BY id").fetchall()
    referenced = {Path(row["file_path"]).resolve(): str(row["id"]) for row in rows}
    missing = [version_id for path, version_id in referenced.items() if not path.is_file()]
    files = set()
    if root.exists():
        if not root.is_dir():
            return result("rag.files_consistency", "error", "The configured RAG files path is not a directory")
        files = {path.resolve() for path in root.rglob("*") if path.is_file()}
    elif referenced:
        missing = list(referenced.values())
    orphaned = sorted(path.name for path in files - set(referenced))
    if missing:
        return result("rag.files_consistency", "error", "Database document versions reference missing files", {
            "missing_count": len(missing),
            "missing_version_samples": missing[:SAMPLE_LIMIT],
            "orphan_count": len(orphaned),
            "orphan_samples": orphaned[:SAMPLE_LIMIT],
        })
    if orphaned:
        return result("rag.files_consistency", "warn", "The RAG files directory contains unreferenced files", {"count": len(orphaned), "samples": orphaned[:SAMPLE_LIMIT]})
    message = "The absent RAG files directory is consistent with an empty database" if not root.exists() else "Database file references and the RAG files directory are consistent"
    return result("rag.files_consistency", "pass", message, {"referenced_count": len(referenced)})


def run_checks() -> dict:
    checks = [
        check_required_env(),
        check_connectivity(),
        _safe_check("migrations.heads", check_migration_heads),
        _safe_check("db.pgvector", check_pgvector),
        _safe_check("agent.checkpoints", check_checkpoints),
        _safe_check("db.role_permissions", check_role_permissions),
        _safe_check("rag.active_revision", check_active_revision),
        _safe_check("rag.embedding_coverage", check_embedding_coverage),
        _safe_check("rag.processing_leases", check_processing_leases),
        _safe_check("agent.run_leases", check_run_leases),
        _safe_check("agent.call_consistency", check_call_consistency),
        _safe_check("rag.files_consistency", check_files_consistency),
    ]
    statuses = {item["status"] for item in checks}
    status = "error" if "error" in statuses else "warning" if "warn" in statuses else "ok"
    return {"schema_version": SCHEMA_VERSION, "status": status, "checks": checks}


def render_text(report: dict) -> str:
    labels = {"pass": "PASS", "warn": "WARN", "error": "ERROR"}
    return "\n".join(f"[{labels[item['status']]}] {item['code']}: {item['message']}" for item in report["checks"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only platform diagnostics")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    report = run_checks()
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(render_text(report))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
