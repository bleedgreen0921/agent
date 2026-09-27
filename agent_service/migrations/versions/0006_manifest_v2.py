from alembic import op


revision = "agent_0006"
down_revision = "agent_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check")
    op.execute("ALTER TABLE agent.run_manifests ADD CONSTRAINT run_manifests_schema_version_check CHECK (schema_version IN (1,2))")


def downgrade():
    op.execute("ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check")
    op.execute("ALTER TABLE agent.run_manifests ADD CONSTRAINT run_manifests_schema_version_check CHECK (schema_version = 1)")
