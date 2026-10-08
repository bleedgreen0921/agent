from alembic import op

revision = "agent_0009"
down_revision = "agent_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE agent.run_experiment_snapshots (
        run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
        schema_version integer NOT NULL CHECK (schema_version = 1),
        snapshot jsonb NOT NULL,
        captured_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("ALTER TABLE agent.evidence_snapshots ALTER COLUMN document_id DROP NOT NULL")
    op.execute("ALTER TABLE agent.evidence_snapshots ALTER COLUMN document_version_id DROP NOT NULL")


def downgrade():
    # Refuse to silently delete experiment citations when restoring the old contract.
    op.execute("ALTER TABLE agent.evidence_snapshots ALTER COLUMN document_id SET NOT NULL")
    op.execute("ALTER TABLE agent.evidence_snapshots ALTER COLUMN document_version_id SET NOT NULL")
    op.execute("DROP TABLE agent.run_experiment_snapshots")
