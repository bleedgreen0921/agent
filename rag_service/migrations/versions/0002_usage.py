"""Stable, authenticated online retrieval budget delegation."""
from alembic import op

revision = 'rag_personal_0002'
down_revision = 'rag_personal_0001'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE rag.search_operations (
        id uuid PRIMARY KEY, run_id text NOT NULL, tool_call_id text NOT NULL,
        request_sha256 text NOT NULL, ticket jsonb, status text NOT NULL,
        result jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '30 days')''')


def downgrade():
    raise RuntimeError('Restore matching code and database backup')
