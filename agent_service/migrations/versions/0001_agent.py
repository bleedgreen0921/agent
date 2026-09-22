from alembic import op

revision = "agent_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")
    op.execute("""CREATE TABLE agent.runs (
        id uuid PRIMARY KEY, team_id uuid NOT NULL, key_id text NOT NULL,
        task text NOT NULL, mode text NOT NULL CHECK (mode IN ('react', 'plan_execute')),
        status text NOT NULL DEFAULT 'queued', created_at timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade():
    op.execute("DROP TABLE agent.runs")
