"""Shared pressure, admission, fair scheduling and attempt progress."""
from alembic import op

revision = 'execution_0002'
down_revision = 'execution_0001'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE execution.flow_buckets (
        scope text NOT NULL, key text NOT NULL, policy_hash text NOT NULL,
        tokens double precision NOT NULL, sampled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        rate_factor double precision NOT NULL DEFAULT 1,
        cooldown_until timestamptz, unhealthy_since timestamptz,
        failures integer NOT NULL DEFAULT 0, successes integer NOT NULL DEFAULT 0,
        generation bigint NOT NULL DEFAULT 0, probe_id uuid, probe_until timestamptz,
        last_error text, PRIMARY KEY(scope,key))""")
    op.execute("""CREATE TABLE execution.downstream_bindings (
        service text PRIMARY KEY, group_key text NOT NULL)""")
    op.execute("""CREATE TABLE execution.claim_cursors (
        kind text PRIMARY KEY, last_recovery boolean NOT NULL DEFAULT true)""")
    op.execute("""CREATE TABLE execution.capacity_reservations (
        run_id uuid PRIMARY KEY REFERENCES execution.tasks(id) ON DELETE CASCADE,
        slots integer NOT NULL CHECK(slots>0))""")
    op.execute("""CREATE TABLE execution.request_permits (
        id uuid PRIMARY KEY, scope text NOT NULL, expires_at timestamptz NOT NULL)""")
    op.execute('CREATE INDEX request_permits_expiry ON execution.request_permits(scope,expires_at)')
    op.execute("""ALTER TABLE execution.tasks ADD COLUMN progress_at timestamptz,
        ADD COLUMN progress_seq bigint NOT NULL DEFAULT 0""")
    op.execute("""CREATE TABLE execution.task_activities (
        task_id uuid NOT NULL, kind text NOT NULL, token uuid NOT NULL, id uuid NOT NULL,
        stage text NOT NULL, started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        deadline_at timestamptz NOT NULL, finished_at timestamptz, waiting_reason text,
        PRIMARY KEY(task_id,token,id),
        FOREIGN KEY(task_id,kind) REFERENCES execution.tasks(id,kind) ON DELETE CASCADE)""")
    op.execute('CREATE INDEX activities_active ON execution.task_activities(task_id,token,deadline_at) WHERE finished_at IS NULL')
    op.execute("""CREATE TABLE execution.flow_alerts (
        scope text NOT NULL, key text NOT NULL, code text NOT NULL,
        first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(), resolved_at timestamptz,
        PRIMARY KEY(scope,key,code))""")
    op.execute("""CREATE TABLE execution.worker_resources (
        instance_id uuid PRIMARY KEY, kind text NOT NULL,
        observed_at timestamptz NOT NULL DEFAULT clock_timestamp(), state jsonb NOT NULL)""")
    for table in ('downstream_bindings', 'worker_resources', 'flow_buckets', 'claim_cursors', 'capacity_reservations', 'request_permits', 'task_activities', 'flow_alerts'):
        op.execute(f'ALTER TABLE execution.{table} ENABLE ROW LEVEL SECURITY')


def downgrade():
    raise RuntimeError('Restore matching code and database backup')
