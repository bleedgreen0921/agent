from alembic import op


revision = "agent_0004"
down_revision = "agent_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE agent.run_manifests (
        run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
        schema_version integer NOT NULL CHECK (schema_version = 1),
        manifest jsonb NOT NULL,
        captured_at timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade():
    op.execute("DROP TABLE agent.run_manifests")
