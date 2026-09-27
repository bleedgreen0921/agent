from alembic import op


revision = "rag_0004"
down_revision = "rag_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE rag.retrieval_audit ADD COLUMN candidate_trace jsonb")


def downgrade():
    op.execute("ALTER TABLE rag.retrieval_audit DROP COLUMN candidate_trace")
