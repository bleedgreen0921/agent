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
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='lifecycle_maintenance'").fetchone():
            conn.execute('CREATE ROLE lifecycle_maintenance NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE')
        if os.environ.get('MAINTENANCE_DATABASE_URL'):
            password=unquote(urlsplit(os.environ['MAINTENANCE_DATABASE_URL']).password or '')
            if not password:raise ValueError('Maintenance database requires a password')
            conn.execute(sql.SQL('ALTER ROLE lifecycle_maintenance LOGIN PASSWORD {}').format(sql.Literal(password)))
        conn.execute('GRANT USAGE ON SCHEMA execution TO lifecycle_maintenance')
        conn.execute('GRANT SELECT ON execution.call_archives TO lifecycle_maintenance')
        conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON execution.retention_holds TO lifecycle_maintenance')
        for function in ('gc_candidates()','gc_run(uuid)','gc_artifact(uuid,text,text)','gc_archive(text,uuid,text)','gc_search(uuid)'):
            conn.execute('GRANT EXECUTE ON FUNCTION execution.'+function+' TO lifecycle_maintenance')
        conn.execute('DROP POLICY IF EXISTS maintenance ON execution.call_archives')
        conn.execute('CREATE POLICY maintenance ON execution.call_archives TO lifecycle_maintenance USING(true)')
        for role, schema in (("rag_runtime", "rag"), ("agent_runtime", "agent")):
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            conn.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        conn.execute("GRANT USAGE ON SCHEMA execution TO agent_runtime, rag_runtime")
        conn.execute("GRANT SELECT, INSERT, UPDATE ON execution.tasks TO agent_runtime, rag_runtime")
        for table in ('token_budgets','token_calls','call_archives','artifacts'):
            conn.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE ON execution.{} TO agent_runtime, rag_runtime').format(sql.Identifier(table)))
            for role,namespace in (('agent_runtime','agent'),('rag_runtime','rag')):
                conn.execute(sql.SQL('DROP POLICY IF EXISTS {} ON execution.{}').format(sql.Identifier(role),sql.Identifier(table)))
                conn.execute(sql.SQL('CREATE POLICY {} ON execution.{} TO {} USING(namespace={}) WITH CHECK(namespace={})').format(
                    sql.Identifier(role),sql.Identifier(table),sql.Identifier(role),sql.Literal(namespace),sql.Literal(namespace)))
        conn.execute('GRANT SELECT, INSERT ON execution.downstream_bindings TO agent_runtime, rag_runtime')
        for role, prefix in (('agent_runtime', 'agent:%'), ('rag_runtime', 'rag:%')):
            conn.execute(sql.SQL('DROP POLICY IF EXISTS {} ON execution.downstream_bindings').format(sql.Identifier(role)))
            conn.execute(sql.SQL('CREATE POLICY {} ON execution.downstream_bindings TO {} USING (service LIKE {}) WITH CHECK (service LIKE {})').format(sql.Identifier(role), sql.Identifier(role), sql.Literal(prefix), sql.Literal(prefix)))
        for table in ('worker_resources', 'flow_buckets', 'claim_cursors', 'capacity_reservations', 'request_permits', 'task_activities', 'flow_alerts'):
            roles = 'agent_runtime' if table == 'capacity_reservations' else 'agent_runtime, rag_runtime'
            conn.execute(sql.SQL('REVOKE ALL ON execution.{} FROM agent_runtime, rag_runtime').format(sql.Identifier(table)))
            privileges = 'SELECT, INSERT, UPDATE, DELETE' if table in ('worker_resources','capacity_reservations','request_permits') else 'SELECT, INSERT, UPDATE'
            conn.execute(sql.SQL('GRANT ' + privileges + ' ON execution.{} TO ' + roles).format(sql.Identifier(table)))
            for role, kinds, scopes in (
                ('agent_runtime', "kind IN ('agent_run','memory_job')", "scope IN ('downstream','agent_run','memory_job')"),
                ('rag_runtime', "kind='rag_document'", "scope IN ('downstream','rag_document','search','upload')")):
                predicate = (kinds if table in ('worker_resources','claim_cursors','task_activities') else
                    'true' if table == 'capacity_reservations' and role == 'agent_runtime' else
                    'false' if table == 'capacity_reservations' else scopes)
                conn.execute(sql.SQL('DROP POLICY IF EXISTS {} ON execution.{}').format(sql.Identifier(role), sql.Identifier(table)))
                conn.execute(sql.SQL('CREATE POLICY {} ON execution.{} TO {} USING ({}) WITH CHECK ({})').format(
                    sql.Identifier(role), sql.Identifier(table), sql.Identifier(role), sql.SQL(predicate), sql.SQL(predicate)))
        for role, predicate in (("agent_runtime", "kind IN ('agent_run','memory_job')"),
                                ("rag_runtime", "kind='rag_document'")):
            conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON execution.tasks").format(sql.Identifier(role)))
            conn.execute(sql.SQL("CREATE POLICY {} ON execution.tasks TO {} USING ({}) WITH CHECK ({})").format(
                sql.Identifier(role), sql.Identifier(role), sql.SQL(predicate), sql.SQL(predicate)))
        # Execution manifests are immutable to the runtime role even though the
        # rest of the Agent schema follows the normal CRUD default privileges.
        conn.execute("REVOKE UPDATE, DELETE ON agent.run_manifests FROM agent_runtime")
        conn.execute("GRANT SELECT, INSERT ON agent.run_manifests TO agent_runtime")
        for table in ("run_goal_contracts", "run_tool_authorizations", "tool_authorization_uses", "run_verifications", "call_checkpoint_receipts", "run_results", "result_claims", "evidence_snapshots", "claim_evidence", "conversation_turns", "conversation_summaries", "personal_facts", "run_memory_snapshots", "run_experiment_snapshots"):
            conn.execute(sql.SQL("REVOKE UPDATE, DELETE ON agent.{} FROM agent_runtime").format(sql.Identifier(table)))
            conn.execute(sql.SQL("GRANT SELECT, INSERT ON agent.{} TO agent_runtime").format(sql.Identifier(table)))
        conn.execute("REVOKE DELETE ON agent.checkpoints,agent.checkpoint_blobs,agent.checkpoint_writes FROM agent_runtime")
        conn.execute('REVOKE UPDATE, DELETE ON agent.execution_bodies,agent.body_pins,agent.verification_batches FROM agent_runtime')
        conn.execute('GRANT SELECT, INSERT ON agent.execution_bodies,agent.body_pins,agent.verification_batches TO agent_runtime')
        conn.execute("REVOKE ALL ON execution.alembic_version, rag.alembic_version, agent.alembic_version FROM rag_runtime, agent_runtime")


if __name__ == "__main__":
    main()
