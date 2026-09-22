from alembic import op

revision = "rag_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""CREATE TABLE rag.documents (
        id uuid PRIMARY KEY, title text NOT NULL,
        visibility text NOT NULL CHECK (visibility IN ('public', 'restricted')),
        deleted_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade():
    op.execute("DROP TABLE rag.documents")
