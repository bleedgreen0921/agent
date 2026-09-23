from alembic import op

revision = "agent_0002"
down_revision = "agent_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE agent.runs RENAME TO agent_runs")
    op.execute("""ALTER TABLE agent.agent_runs
        ADD COLUMN request_digest bytea,
        ADD COLUMN idempotency_key text,
        ADD COLUMN queue_deadline_at timestamptz,
        ADD COLUMN started_at timestamptz,
        ADD COLUMN execution_deadline_at timestamptz,
        ADD COLUMN finished_at timestamptz,
        ADD COLUMN lease_token uuid,
        ADD COLUMN leased_until timestamptz,
        ADD COLUMN heartbeat_at timestamptz,
        ADD COLUMN cancellation_requested_at timestamptz,
        ADD COLUMN model_calls_used integer NOT NULL DEFAULT 0,
        ADD COLUMN tool_calls_used integer NOT NULL DEFAULT 0,
        ADD COLUMN model_call_limit integer NOT NULL DEFAULT 16,
        ADD COLUMN tool_call_limit integer NOT NULL DEFAULT 10,
        ADD COLUMN termination_reason text,
        ADD COLUMN error_code text,
        ADD COLUMN graph_version text NOT NULL DEFAULT 'v1'""")
    op.execute("UPDATE agent.agent_runs SET request_digest=decode(repeat('00',32),'hex'), queue_deadline_at=created_at+interval '60 seconds'")
    op.execute("ALTER TABLE agent.agent_runs ALTER COLUMN request_digest SET NOT NULL, ALTER COLUMN queue_deadline_at SET NOT NULL")
    op.execute("ALTER TABLE agent.agent_runs ADD CONSTRAINT agent_runs_status_check CHECK (status IN ('queued','running','cancelling','completed','partial','failed','cancelled'))")
    op.execute("CREATE UNIQUE INDEX agent_runs_idempotency ON agent.agent_runs(team_id,idempotency_key) WHERE idempotency_key IS NOT NULL")
    op.execute("CREATE INDEX agent_runs_claim_idx ON agent.agent_runs(created_at) WHERE status IN ('queued','running')")
    op.execute("""CREATE TABLE agent.run_results (
        id uuid PRIMARY KEY, run_id uuid NOT NULL UNIQUE REFERENCES agent.agent_runs(id),
        answer text NOT NULL, notices jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE agent.result_claims (
        id uuid PRIMARY KEY, result_id uuid NOT NULL REFERENCES agent.run_results(id) ON DELETE CASCADE,
        ordinal integer NOT NULL, text text NOT NULL,
        support text NOT NULL CHECK (support IN ('evidence','unverified')),
        reason text, UNIQUE(result_id,ordinal), UNIQUE(id,result_id)
    )""")
    op.execute("""CREATE TABLE agent.evidence_snapshots (
        result_id uuid NOT NULL REFERENCES agent.run_results(id) ON DELETE CASCADE,
        evidence_id text NOT NULL, document_id text NOT NULL, document_version_id text NOT NULL,
        title text NOT NULL, content text NOT NULL, source_locator jsonb NOT NULL,
        PRIMARY KEY(result_id,evidence_id)
    )""")
    op.execute("""CREATE TABLE agent.claim_evidence (
        claim_id uuid NOT NULL,
        result_id uuid NOT NULL, evidence_id text NOT NULL,
        PRIMARY KEY(claim_id,evidence_id),
        FOREIGN KEY(claim_id,result_id) REFERENCES agent.result_claims(id,result_id) ON DELETE CASCADE,
        FOREIGN KEY(result_id,evidence_id) REFERENCES agent.evidence_snapshots(result_id,evidence_id)
    )""")
    op.execute("""CREATE TABLE agent.run_steps (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        ordinal integer NOT NULL, goal text NOT NULL, completion_condition text NOT NULL,
        status text NOT NULL, result_summary text,
        started_at timestamptz, finished_at timestamptz, error_code text,
        UNIQUE(run_id,ordinal)
    )""")
    op.execute("""CREATE TABLE agent.model_calls (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        step_id uuid REFERENCES agent.run_steps(id), purpose text NOT NULL,
        model text NOT NULL, status text NOT NULL,
        started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
        input_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
        output_summary jsonb, input_tokens integer, output_tokens integer,
        error_code text
    )""")
    op.execute("""CREATE TABLE agent.tool_calls (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
        step_id uuid REFERENCES agent.run_steps(id), triggering_model_call_id uuid REFERENCES agent.model_calls(id),
        tool_name text NOT NULL, status text NOT NULL,
        started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
        argument_summary jsonb NOT NULL DEFAULT '{}'::jsonb, result_summary jsonb,
        service_request_id text, retrieval_id text, evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        error_code text
    )""")


def downgrade():
    for table in ("claim_evidence", "evidence_snapshots", "result_claims", "run_results", "tool_calls", "model_calls", "run_steps"):
        op.execute(f"DROP TABLE agent.{table}")
    op.execute("DROP INDEX agent.agent_runs_claim_idx")
    op.execute("DROP INDEX agent.agent_runs_idempotency")
    op.execute("ALTER TABLE agent.agent_runs DROP CONSTRAINT agent_runs_status_check")
    for column in ("request_digest", "idempotency_key", "queue_deadline_at", "started_at", "execution_deadline_at", "finished_at", "lease_token", "leased_until", "heartbeat_at", "cancellation_requested_at", "model_calls_used", "tool_calls_used", "model_call_limit", "tool_call_limit", "termination_reason", "error_code", "graph_version"):
        op.execute(f"ALTER TABLE agent.agent_runs DROP COLUMN {column}")
    op.execute("ALTER TABLE agent.agent_runs RENAME TO runs")
