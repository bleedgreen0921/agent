"""Create least-privilege login roles. Run against a fresh database as owner."""

import os

import psycopg
from psycopg import sql


ROLES = {
    "RAG_DATABASE_URL": "rag_runtime",
    "AGENT_DATABASE_URL": "agent_runtime",
    "IDENTITY_ADMIN_DATABASE_URL": "identity_admin",
}


def main() -> None:
    from urllib.parse import urlsplit, unquote

    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"], autocommit=True) as conn:
        for env, role in ROLES.items():
            password = unquote(urlsplit(os.environ[env]).password or "")
            if not password:
                raise ValueError(f"{env} must include a password")
            row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if not row:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
            else:
                conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        for schema in ("identity", "rag", "agent"):
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        for role in ROLES.values():
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(conn.info.dbname), sql.Identifier(role)))
        for role, schema in (("rag_runtime", "rag"), ("agent_runtime", "agent"), ("identity_admin", "identity")):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        for role in ("rag_runtime", "agent_runtime"):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA identity TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT ON identity.teams, identity.credentials TO {}").format(sql.Identifier(role)))
        conn.execute("REVOKE ALL ON identity.alembic_version, rag.alembic_version, agent.alembic_version FROM rag_runtime, agent_runtime, identity_admin")


if __name__ == "__main__":
    main()
