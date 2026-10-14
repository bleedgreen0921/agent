"""New tool distribution semantics; historical manifests remain immutable."""
from alembic import op

revision = 'agent_personal_0004'
down_revision = 'agent_personal_0003'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent.agent_runs ALTER COLUMN graph_version SET DEFAULT 'v3'")
    op.execute("""UPDATE agent.agent_runs r SET graph_version='v3' FROM execution.tasks t
        WHERE t.id=r.id AND t.status='queued' AND t.started_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM agent.run_manifests m WHERE m.run_id=r.id)""")


def downgrade():
    raise RuntimeError('Restore matching code and database backup')
