from alembic import op


revision = "rag_0003"
down_revision = "rag_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE rag.retrieval_audit
        ADD COLUMN operation text,
        ADD COLUMN retrieval_id text,
        ADD COLUMN query_sha256 text,
        ADD COLUMN rewritten_query_sha256 text,
        ADD COLUMN selected_evidence_ids jsonb,
        ADD COLUMN error_code text""")
    op.execute("""UPDATE rag.retrieval_audit
        SET operation = CASE WHEN status IN ('read_ok', 'not_found') THEN 'read' ELSE 'search' END""")
    op.execute("ALTER TABLE rag.retrieval_audit ALTER COLUMN operation SET NOT NULL")
    op.execute("ALTER TABLE rag.retrieval_audit ADD CONSTRAINT retrieval_audit_operation_check CHECK (operation IN ('search', 'read'))")
    op.execute("CREATE UNIQUE INDEX retrieval_audit_retrieval_id_idx ON rag.retrieval_audit(retrieval_id) WHERE retrieval_id IS NOT NULL")
    op.execute("CREATE INDEX retrieval_audit_run_tool_created_idx ON rag.retrieval_audit(run_id,tool_call_id,created_at)")


def downgrade():
    op.execute("DROP INDEX rag.retrieval_audit_run_tool_created_idx")
    op.execute("DROP INDEX rag.retrieval_audit_retrieval_id_idx")
    op.execute("ALTER TABLE rag.retrieval_audit DROP CONSTRAINT retrieval_audit_operation_check")
    for column in (
        "operation",
        "retrieval_id",
        "query_sha256",
        "rewritten_query_sha256",
        "selected_evidence_ids",
        "error_code",
    ):
        op.execute(f"ALTER TABLE rag.retrieval_audit DROP COLUMN {column}")
