"""Fresh personal instance baseline. Historical databases are deliberately unsupported."""
from alembic import op

revision = 'execution_0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS execution')
    op.execute("""
        CREATE TABLE execution.tasks (
         id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('agent_run','memory_job','rag_document')),
         contract_version integer NOT NULL DEFAULT 1 CHECK(contract_version=1),
         status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','stopping','completed','partial','failed','cancelled')),
         error_code text, termination_reason text,
         created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
         available_at timestamptz NOT NULL DEFAULT clock_timestamp(), queue_deadline_at timestamptz,
         started_at timestamptz, finished_at timestamptz, execution_deadline_at timestamptz,
         lease_token uuid, leased_until timestamptz, heartbeat_at timestamptz, worker_id uuid,
         attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0), max_attempts integer NOT NULL CHECK(max_attempts>0),
         execution_seconds integer CHECK(execution_seconds>0), stop_requested_at timestamptz, stop_reason text,
         UNIQUE(id,kind), CHECK(status<>'partial' OR kind='agent_run'),
         CHECK(status NOT IN ('running','stopping') OR (lease_token IS NOT NULL AND leased_until IS NOT NULL AND execution_deadline_at IS NOT NULL)),
         CHECK((started_at IS NULL) = (execution_deadline_at IS NULL)))
    """)
    op.execute("CREATE INDEX tasks_claim_idx ON execution.tasks(kind,available_at,created_at,id) WHERE status IN ('queued','running','stopping')")
    op.execute('CREATE INDEX tasks_list_idx ON execution.tasks(kind,created_at DESC,id DESC)')
    op.execute('ALTER TABLE execution.tasks ENABLE ROW LEVEL SECURITY')

def downgrade():
    raise RuntimeError("Personal baselines cannot be downgraded; restore the archived database with its matching code")
