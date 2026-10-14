"""Async execution protocol and immutable checkpoint receipts; retain personal history."""
from alembic import op
revision = 'agent_personal_0002'
down_revision = 'agent_personal_0001'
branch_labels = None
depends_on = None

RUN_STATE_VIEW_SQL = """CREATE OR REPLACE VIEW agent.run_state WITH (security_invoker=true) AS
    SELECT r.id,r.task_kind,r.task,r.mode,r.request_digest,r.idempotency_key,r.graph_version,
    r.model_calls_used,r.tool_calls_used,r.model_call_limit,r.tool_call_limit,
    t.contract_version,CASE WHEN t.status='stopping' THEN 'cancelling' ELSE t.status END AS status,
    t.error_code,t.termination_reason,t.created_at,t.updated_at,t.available_at,t.queue_deadline_at,
    t.started_at,t.finished_at,t.execution_deadline_at,t.lease_token,t.leased_until,t.heartbeat_at,
    t.worker_id,t.attempts,t.max_attempts,t.execution_seconds,t.stop_requested_at,t.stop_reason,r.recovery_protocol
    FROM agent.agent_runs r JOIN execution.tasks t ON t.id=r.id AND t.kind=r.task_kind"""


def upgrade():
    op.execute('ALTER TABLE agent.agent_runs ADD COLUMN recovery_protocol integer NOT NULL DEFAULT 1 CHECK (recovery_protocol IN (1,2))')
    op.execute("ALTER TABLE agent.agent_runs ALTER COLUMN recovery_protocol SET DEFAULT 2")
    op.execute("ALTER TABLE agent.run_manifests DROP CONSTRAINT run_manifests_schema_version_check")
    op.execute("ALTER TABLE agent.run_manifests ADD CHECK (schema_version IN (1,2,3))")
    op.execute(RUN_STATE_VIEW_SQL)
    op.execute('ALTER TABLE agent.worker_instances RENAME COLUMN memory_child_alive TO memory_consumer_alive')
    for table in ('model_calls','tool_calls'):
        op.execute(f'ALTER TABLE agent.{table} ADD COLUMN attempt_token uuid')
        op.execute(f'ALTER TABLE agent.{table} ADD UNIQUE (run_id,id)')
    op.execute('''CREATE TABLE agent.call_checkpoint_receipts (
        run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        model_call_id uuid, tool_call_id uuid,
        source text NOT NULL CHECK(source IN ('checkpoint','pending_write')),
        thread_id text NOT NULL, checkpoint_ns text NOT NULL, checkpoint_id text NOT NULL,
        graph_task_id text NOT NULL DEFAULT '', write_idx integer NOT NULL DEFAULT -1,
        channel text NOT NULL, payload_sha256 bytea NOT NULL CHECK(octet_length(payload_sha256)=32),
        recovery_protocol integer NOT NULL DEFAULT 2 CHECK(recovery_protocol=2),
        confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK ((model_call_id IS NULL) <> (tool_call_id IS NULL)),
        CHECK ((source='checkpoint' AND graph_task_id='' AND write_idx=-1) OR
               (source='pending_write' AND graph_task_id<>'')),
        FOREIGN KEY(run_id,model_call_id) REFERENCES agent.model_calls(run_id,id),
        FOREIGN KEY(run_id,tool_call_id) REFERENCES agent.tool_calls(run_id,id))''')
    op.execute('''CREATE UNIQUE INDEX call_receipts_anchor_idx ON agent.call_checkpoint_receipts
        (run_id,COALESCE(model_call_id,tool_call_id),(model_call_id IS NOT NULL),source,thread_id,checkpoint_ns,checkpoint_id,graph_task_id,write_idx,channel)''')
    op.execute('CREATE INDEX call_receipts_run_idx ON agent.call_checkpoint_receipts(run_id)')
    op.execute("UPDATE agent.agent_runs r SET recovery_protocol=2,graph_version='v2' FROM execution.tasks t WHERE t.id=r.id AND t.kind='agent_run' AND t.status='queued' AND t.started_at IS NULL")


def downgrade():
    raise RuntimeError('Restore the second-stage code and matching database backup')
