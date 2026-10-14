"""Token reservations, diagnostic archives and managed artifact ownership."""
from alembic import op

revision = 'execution_0003'
down_revision = 'execution_0002'
branch_labels = depends_on = None


def upgrade():
    op.execute('''CREATE TABLE execution.token_budgets (
        namespace text NOT NULL CHECK(namespace IN ('agent','rag')), id text NOT NULL,
        task_id uuid REFERENCES execution.tasks(id), kind text NOT NULL,
        policy jsonb NOT NULL, token_limit bigint NOT NULL CHECK(token_limit>0),
        charged bigint NOT NULL DEFAULT 0 CHECK(charged>=0), blocked boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(namespace,id))''')
    op.execute('''CREATE TABLE execution.token_calls (
        namespace text NOT NULL, budget_id text NOT NULL, id uuid NOT NULL,
        model text NOT NULL, service text NOT NULL, request_sha256 text NOT NULL,
        input_estimate bigint NOT NULL, output_reserved bigint NOT NULL,
        reserved bigint NOT NULL, charged bigint NOT NULL,
        input_tokens bigint, output_tokens bigint, estimation text NOT NULL,
        status text NOT NULL CHECK(status IN ('reserved','sent','settled','unknown','released')),
        attempt_token uuid, parent_call_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
        PRIMARY KEY(namespace,id), FOREIGN KEY(namespace,budget_id) REFERENCES execution.token_budgets(namespace,id))''')
    op.execute('CREATE INDEX token_calls_budget ON execution.token_calls(namespace,budget_id)')
    op.execute('''CREATE TABLE execution.call_archives (
        namespace text NOT NULL, id uuid NOT NULL, task_id uuid REFERENCES execution.tasks(id),
        call_id uuid, run_reference text, service text NOT NULL, input_path text NOT NULL, input_sha256 text NOT NULL,
        output_path text, output_sha256 text, status text NOT NULL DEFAULT 'prepared',
        error_type text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '30 days',
        PRIMARY KEY(namespace,id))''')
    op.execute('''CREATE TABLE execution.artifacts (
        namespace text NOT NULL, id uuid NOT NULL, path text NOT NULL, target_path text,
        state text NOT NULL CHECK(state IN ('writing','prepared','referenced','unknown','quarantined','deleted')),
        writer_id uuid NOT NULL, writer_until timestamptz NOT NULL,
        sha256 text, quarantine_path text, hold boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), quarantined_at timestamptz,
        PRIMARY KEY(namespace,id), UNIQUE(namespace,path))''')
    for table in ('token_budgets','token_calls','call_archives','artifacts'):
        op.execute(f'ALTER TABLE execution.{table} ENABLE ROW LEVEL SECURITY')


def downgrade():
    raise RuntimeError('Restore matching code and database backup')
