"""Bounded, read-only database probes for API readiness."""

import os

import psycopg


AGENT_TABLES = (
    'execution.token_budgets','execution.token_calls','execution.call_archives','execution.artifacts',
    'agent.execution_bodies','agent.body_pins','agent.verification_batches',
    "agent.run_goal_contracts", "agent.run_tool_authorizations", "agent.tool_authorization_uses", "agent.run_verifications",
    "execution.worker_resources", "execution.flow_buckets", "execution.downstream_bindings", "execution.claim_cursors",
    "execution.capacity_reservations", "execution.request_permits", "execution.task_activities", "execution.flow_alerts",
    "execution.tasks", "agent.agent_runs", "agent.run_results",
    "agent.result_claims", "agent.evidence_snapshots", "agent.claim_evidence",
    "agent.run_steps", "agent.model_calls", "agent.tool_calls", "agent.run_manifests",
    "agent.checkpoints", "agent.checkpoint_blobs", "agent.checkpoint_writes",
    "agent.conversations", "agent.conversation_turns", "agent.conversation_summaries",
    "agent.personal_facts", "agent.memory_jobs", "agent.run_memory_snapshots",
    "agent.worker_instances", "agent.run_experiment_snapshots", "agent.call_checkpoint_receipts",
)
RAG_TABLES = (
    'execution.token_budgets','execution.token_calls','execution.call_archives','execution.artifacts','rag.search_operations',
    "execution.worker_resources", "execution.flow_buckets", "execution.downstream_bindings", "execution.claim_cursors",
    "execution.request_permits", "execution.task_activities", "execution.flow_alerts",
    "execution.tasks", "rag.documents", "rag.document_versions", "rag.upload_keys", "rag.processing_jobs", "rag.chunks",
    "rag.index_revisions", "rag.index_state", "rag.embeddings", "rag.retrieval_audit",
    "rag.worker_instances",
)


def _probe(env_name: str, tables: tuple[str, ...], *, active_index: bool = False) -> bool:
    try:
        with psycopg.connect(
            os.environ[env_name], connect_timeout=2,
            options="-c statement_timeout=2000 -c default_transaction_read_only=on",
        ) as conn:
            for table in tables:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 0")
            if active_index:
                row = conn.execute("""SELECT 1 FROM rag.index_state s
                    JOIN rag.index_revisions r ON r.id=s.active_revision_id
                    WHERE s.singleton=true AND r.state='active' LIMIT 1""").fetchone()
                return row is not None
        return True
    except (KeyError, psycopg.Error, OSError, ValueError):
        return False


def agent_ready() -> bool:
    return _probe("AGENT_DATABASE_URL", AGENT_TABLES)


def rag_ready() -> bool:
    if len(os.environ.get("RAG_SERVICE_TOKEN", "")) < 43:
        return False
    return _probe("RAG_DATABASE_URL", RAG_TABLES, active_index=True)
