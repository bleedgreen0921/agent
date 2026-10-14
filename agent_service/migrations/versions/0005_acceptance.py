"""Goal acceptance, explicit tool authority and immutable verification history."""
from alembic import op

revision = 'agent_personal_0005'
down_revision = 'agent_personal_0004'
branch_labels = None
depends_on = None

def upgrade():
    op.execute('ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check')
    op.execute('ALTER TABLE agent.run_manifests ADD CONSTRAINT run_manifests_schema_version_check CHECK (schema_version IN (1,2,3,4,5))')
    op.execute("ALTER TABLE agent.agent_runs ALTER COLUMN graph_version SET DEFAULT 'v4'")
    op.execute("""UPDATE agent.agent_runs r SET graph_version='v4' FROM execution.tasks t
        WHERE t.id=r.id AND t.status='queued' AND t.started_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM agent.run_manifests m WHERE m.run_id=r.id)""")
    op.execute('ALTER TABLE agent.run_results ADD COLUMN acceptance jsonb')
    op.execute("ALTER TABLE agent.run_steps ADD COLUMN requirement_ids jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute('''CREATE TABLE agent.run_goal_contracts (
        run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id), task_sha256 text NOT NULL,
        contract jsonb NOT NULL, contract_sha256 text NOT NULL,
        model_call_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(run_id,model_call_id) REFERENCES agent.model_calls(run_id,id))''')
    op.execute('''CREATE TABLE agent.run_tool_authorizations (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        ordinal integer NOT NULL, grant_data jsonb NOT NULL,
        UNIQUE(run_id,ordinal), UNIQUE(run_id,id))''')
    op.execute('''CREATE TABLE agent.tool_authorization_uses (
        run_id uuid NOT NULL, authorization_id uuid NOT NULL, tool_call_id uuid NOT NULL,
        PRIMARY KEY(tool_call_id),
        FOREIGN KEY(run_id,authorization_id) REFERENCES agent.run_tool_authorizations(run_id,id),
        FOREIGN KEY(run_id,tool_call_id) REFERENCES agent.tool_calls(run_id,id))''')
    op.execute('''CREATE TABLE agent.run_verifications (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        phase text NOT NULL CHECK(phase IN ('tool','step','final')), scope_key text NOT NULL,
        input_sha256 text NOT NULL, payload jsonb NOT NULL,
        model_call_id uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(run_id,phase,scope_key),
        FOREIGN KEY(run_id,model_call_id) REFERENCES agent.model_calls(run_id,id))''')

def downgrade():
    raise RuntimeError('Restore matching code and database backup')
