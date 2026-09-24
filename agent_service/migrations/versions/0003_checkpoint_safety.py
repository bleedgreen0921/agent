from alembic import op


revision = "agent_0003"
down_revision = "agent_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent.model_calls ADD COLUMN checkpointed boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE agent.tool_calls ADD COLUMN checkpointed boolean NOT NULL DEFAULT false")


def downgrade():
    op.execute("ALTER TABLE agent.tool_calls DROP COLUMN checkpointed")
    op.execute("ALTER TABLE agent.model_calls DROP COLUMN checkpointed")
