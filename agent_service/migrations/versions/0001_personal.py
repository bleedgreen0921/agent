"""Fresh personal instance baseline. Historical databases are deliberately unsupported."""
from alembic import op

revision = 'agent_personal_0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS agent')
    op.execute("""
        CREATE TABLE agent.agent_runs (
         id uuid PRIMARY KEY, task_kind text NOT NULL DEFAULT 'agent_run' CHECK(task_kind='agent_run'),
         task text NOT NULL, mode text NOT NULL CHECK(mode IN ('react','plan_execute')),
         request_digest bytea NOT NULL, idempotency_key text UNIQUE, graph_version text NOT NULL,
         model_calls_used integer NOT NULL DEFAULT 0, tool_calls_used integer NOT NULL DEFAULT 0,
         model_call_limit integer NOT NULL DEFAULT 16, tool_call_limit integer NOT NULL DEFAULT 10,
         FOREIGN KEY(id,task_kind) REFERENCES execution.tasks(id,kind))
    """)
    op.execute("""
        CREATE TABLE agent.run_results (
                id uuid PRIMARY KEY, run_id uuid NOT NULL UNIQUE REFERENCES agent.agent_runs(id),
                answer text NOT NULL, notices jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        CREATE TABLE agent.result_claims (
                id uuid PRIMARY KEY, result_id uuid NOT NULL REFERENCES agent.run_results(id) ON DELETE CASCADE,
                ordinal integer NOT NULL, text text NOT NULL,
                support text NOT NULL CHECK (support IN ('evidence','unverified')),
                reason text, UNIQUE(result_id,ordinal), UNIQUE(id,result_id)
            )
    """)
    op.execute("""
        CREATE TABLE agent.evidence_snapshots (
                result_id uuid NOT NULL REFERENCES agent.run_results(id) ON DELETE CASCADE,
                evidence_id text NOT NULL, document_id text, document_version_id text,
                title text NOT NULL, content text NOT NULL, source_locator jsonb NOT NULL,
                PRIMARY KEY(result_id,evidence_id)
            )
    """)
    op.execute("""
        CREATE TABLE agent.claim_evidence (
                claim_id uuid NOT NULL,
                result_id uuid NOT NULL, evidence_id text NOT NULL,
                PRIMARY KEY(claim_id,evidence_id),
                FOREIGN KEY(claim_id,result_id) REFERENCES agent.result_claims(id,result_id) ON DELETE CASCADE,
                FOREIGN KEY(result_id,evidence_id) REFERENCES agent.evidence_snapshots(result_id,evidence_id)
            )
    """)
    op.execute("""
        CREATE TABLE agent.run_steps (
                id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
                ordinal integer NOT NULL, goal text NOT NULL, completion_condition text NOT NULL,
                status text NOT NULL, result_summary text,
                started_at timestamptz, finished_at timestamptz, error_code text,
                UNIQUE(run_id,ordinal)
            )
    """)
    op.execute("""
        CREATE TABLE agent.model_calls (
                id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
                step_id uuid REFERENCES agent.run_steps(id), purpose text NOT NULL,
                model text NOT NULL, status text NOT NULL,
                started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
                input_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
                output_summary jsonb, input_tokens integer, output_tokens integer,
                error_code text, checkpointed boolean NOT NULL DEFAULT false
            )
    """)
    op.execute("""
        CREATE TABLE agent.tool_calls (
                id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent.agent_runs(id),
                step_id uuid REFERENCES agent.run_steps(id), triggering_model_call_id uuid REFERENCES agent.model_calls(id),
                tool_name text NOT NULL, status text NOT NULL,
                started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
                argument_summary jsonb NOT NULL DEFAULT '{}'::jsonb, result_summary jsonb,
                service_request_id text, retrieval_id text, evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                error_code text, checkpointed boolean NOT NULL DEFAULT false
            )
    """)
    op.execute("""
        CREATE TABLE agent.run_manifests (
                run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
                schema_version integer NOT NULL CHECK (schema_version IN (1,2)),
                manifest jsonb NOT NULL,
                captured_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        CREATE TABLE agent.conversations (
                id uuid PRIMARY KEY,
                created_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        CREATE TABLE agent.conversation_turns (
                id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
                ordinal integer NOT NULL CHECK (ordinal>0), run_id uuid NOT NULL UNIQUE REFERENCES agent.agent_runs(id),
                user_text text NOT NULL, idempotency_key text NOT NULL, request_digest bytea NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,ordinal),
                UNIQUE(conversation_id,idempotency_key)
            )
    """)
    op.execute('CREATE INDEX turns_conversation_idx ON agent.conversation_turns(conversation_id,ordinal DESC)')
    op.execute("""
        CREATE TABLE agent.conversation_summaries (
                id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
                version integer NOT NULL, through_ordinal integer NOT NULL, content text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,version),
                UNIQUE(conversation_id,through_ordinal)
            )
    """)
    op.execute("""
        CREATE TABLE agent.personal_facts (
                id uuid PRIMARY KEY,
                source_turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id),
                statement text NOT NULL, source_quote text NOT NULL, subject text NOT NULL,
                subject_quote text NOT NULL, subject_turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id),
                stated_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                embedding vector, embedding_model text,
                UNIQUE(source_turn_id,statement,source_quote)
            )
    """)
    op.execute('CREATE INDEX personal_facts_subject_idx ON agent.personal_facts(subject,stated_at DESC)')
    op.execute("""
        CREATE TABLE agent.run_memory_snapshots (
                run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
                snapshot jsonb NOT NULL, captured_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        CREATE TABLE agent.worker_instances (
                instance_id uuid PRIMARY KEY,
                started_at timestamptz NOT NULL DEFAULT now(),
                heartbeat_at timestamptz NOT NULL DEFAULT now(),
                memory_child_alive boolean NOT NULL DEFAULT false
            )
    """)
    op.execute('CREATE INDEX agent_worker_heartbeat_idx ON agent.worker_instances(heartbeat_at)')
    op.execute("""
        CREATE TABLE agent.run_experiment_snapshots (
                run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
                schema_version integer NOT NULL CHECK (schema_version = 1),
                snapshot jsonb NOT NULL,
                captured_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        CREATE TABLE agent.memory_jobs (
         id uuid PRIMARY KEY, task_kind text NOT NULL DEFAULT 'memory_job' CHECK(task_kind='memory_job'),
         kind text NOT NULL CHECK(kind IN ('extract','summary')),
         turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id), UNIQUE(kind,turn_id),
         FOREIGN KEY(id,task_kind) REFERENCES execution.tasks(id,kind))
    """)
    op.execute("""
        CREATE VIEW agent.run_state WITH (security_invoker=true) AS
        SELECT r.*,t.contract_version,CASE WHEN t.status='stopping' THEN 'cancelling' ELSE t.status END AS status,t.error_code,t.termination_reason,t.created_at,t.updated_at,t.available_at,t.queue_deadline_at,t.started_at,t.finished_at,t.execution_deadline_at,t.lease_token,t.leased_until,t.heartbeat_at,t.worker_id,t.attempts,t.max_attempts,t.execution_seconds,t.stop_requested_at,t.stop_reason
        FROM agent.agent_runs r
        JOIN execution.tasks t ON t.id=r.id AND t.kind=r.task_kind
    """)
    op.execute("""
        CREATE VIEW agent.memory_job_state WITH (security_invoker=true) AS
        SELECT j.*,CASE WHEN t.status='queued' THEN 'pending' WHEN t.status='completed' THEN 'done' ELSE t.status END AS status,t.attempts,t.max_attempts,t.created_at,t.error_code,t.execution_deadline_at,t.lease_token,t.leased_until
        FROM agent.memory_jobs j
        JOIN execution.tasks t ON t.id=j.id AND t.kind=j.task_kind
    """)

def downgrade():
    raise RuntimeError("Personal baselines cannot be downgraded; restore the archived database with its matching code")
