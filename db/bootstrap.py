"""Create least-privilege login roles. Run against a fresh database as owner."""

import os

import psycopg
from psycopg import sql


ROLES = {
    "RAG_DATABASE_URL": "rag_runtime",
    "AGENT_DATABASE_URL": "agent_runtime",
}


def main() -> None:
    from urllib.parse import urlsplit, unquote

    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"], autocommit=True) as conn:
        from db.migrate import validate_database
        validate_database(conn)
        for env, role in ROLES.items():
            password = unquote(urlsplit(os.environ[env]).password or "")
            if not password:
                raise ValueError(f"{env} must include a password")
            row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if not row:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
            else:
                conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        # Both services resolve pgvector types; neither can access the other's data.
        conn.execute("GRANT USAGE ON SCHEMA public TO rag_runtime, agent_runtime")
        for schema in ("execution", "rag", "agent"):
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        for role in ROLES.values():
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(conn.info.dbname), sql.Identifier(role)))
        for role, schema in (("rag_runtime", "rag"), ("agent_runtime", "agent")):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        conn.execute("GRANT USAGE ON SCHEMA execution TO agent_runtime, rag_runtime")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON execution.tasks TO agent_runtime, rag_runtime")
        for role, predicate in (("agent_runtime", "kind IN ('agent_run','memory_job')"),
                                ("rag_runtime", "kind='rag_document'")):
            conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON execution.tasks").format(sql.Identifier(role)))
            conn.execute(sql.SQL("CREATE POLICY {} ON execution.tasks TO {} USING ({}) WITH CHECK ({})").format(
                sql.Identifier(role), sql.Identifier(role), sql.SQL(predicate), sql.SQL(predicate)))
        # Execution manifests are immutable to the runtime role even though the
        # rest of the Agent schema follows the normal CRUD default privileges.
        conn.execute("REVOKE UPDATE, DELETE ON agent.run_manifests FROM agent_runtime")
        conn.execute("GRANT SELECT, INSERT ON agent.run_manifests TO agent_runtime")
        for table in ("call_checkpoint_receipts", "conversation_turns", "conversation_summaries", "personal_facts", "run_memory_snapshots", "run_experiment_snapshots"):
            conn.execute(sql.SQL("REVOKE UPDATE, DELETE ON agent.{} FROM agent_runtime").format(sql.Identifier(table)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT ON agent.{} TO agent_runtime").format(sql.Identifier(table)))
        conn.execute("REVOKE ALL ON execution.alembic_version, rag.alembic_version, agent.alembic_version FROM rag_runtime, agent_runtime")


if __name__ == "__main__":
    main()
