"""Bounded, read-only database probes for API readiness."""

import os

import psycopg


AGENT_TABLES = (
    "identity.credentials", "identity.teams", "identity.users", "agent.agent_runs", "agent.run_results",
    "agent.result_claims", "agent.evidence_snapshots", "agent.claim_evidence",
    "agent.run_steps", "agent.model_calls", "agent.tool_calls", "agent.run_manifests",
    "agent.checkpoints", "agent.checkpoint_blobs", "agent.checkpoint_writes",
    "agent.conversations", "agent.conversation_turns", "agent.conversation_summaries",
    "agent.personal_facts", "agent.memory_jobs", "agent.run_memory_snapshots",
)
RAG_TABLES = (
    "identity.credentials", "identity.teams", "identity.users", "rag.documents", "rag.document_access",
    "rag.document_versions", "rag.upload_keys", "rag.processing_jobs", "rag.chunks",
    "rag.index_revisions", "rag.index_state", "rag.embeddings", "rag.retrieval_audit",
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
