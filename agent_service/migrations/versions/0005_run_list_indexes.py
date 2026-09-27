from alembic import op


revision = "agent_0005"
down_revision = "agent_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE INDEX agent_runs_team_list_idx ON agent.agent_runs(team_id, created_at DESC, id DESC)")
    op.execute("CREATE INDEX agent_runs_admin_list_idx ON agent.agent_runs(created_at DESC, id DESC)")


def downgrade():
    op.execute("DROP INDEX agent.agent_runs_admin_list_idx")
    op.execute("DROP INDEX agent.agent_runs_team_list_idx")
