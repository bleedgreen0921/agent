"""Immutable graph bodies and explicit retention pins."""
from alembic import op

revision = 'agent_personal_0006'
down_revision = 'agent_personal_0005'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE agent.verification_batches (
        run_id uuid NOT NULL, call_id uuid PRIMARY KEY, input_sha256 text NOT NULL,
        value jsonb NOT NULL, parts jsonb NOT NULL,
        FOREIGN KEY(run_id,call_id) REFERENCES agent.model_calls(run_id,id))''')
    op.execute('''CREATE TABLE agent.execution_bodies (
        run_id uuid NOT NULL REFERENCES agent.agent_runs(id), sha256 text NOT NULL,
        format text NOT NULL, body bytea NOT NULL, byte_length bigint NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(run_id,sha256))''')
    op.execute('''CREATE TABLE agent.body_pins (
        run_id uuid NOT NULL, sha256 text NOT NULL, owner text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(run_id,sha256,owner),
        FOREIGN KEY(run_id,sha256) REFERENCES agent.execution_bodies(run_id,sha256))''')
    op.execute('ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check')
    op.execute('ALTER TABLE agent.run_manifests ADD CONSTRAINT run_manifests_schema_version_check CHECK(schema_version IN (1,2,3,4,5,6))')
    op.execute("ALTER TABLE agent.agent_runs ALTER COLUMN graph_version SET DEFAULT 'v5'")
    op.execute("""UPDATE agent.agent_runs r SET graph_version='v5' FROM execution.tasks t
        WHERE t.id=r.id AND t.status='queued' AND t.started_at IS NULL
        AND NOT EXISTS(SELECT 1 FROM agent.run_manifests m WHERE m.run_id=r.id)""")


def downgrade():
    raise RuntimeError('Restore matching code and database backup')
