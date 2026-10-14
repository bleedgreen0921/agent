"""Allow complete recovery manifests; retain old history without state conversion."""
from alembic import op

revision = 'agent_personal_0003'
down_revision = 'agent_personal_0002'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check')
    op.execute('ALTER TABLE agent.run_manifests ADD CONSTRAINT run_manifests_schema_version_check CHECK (schema_version IN (1,2,3,4))')


def downgrade():
    raise RuntimeError('Restore the matching code and database backup; execution state is not converted')
