from alembic import op

revision = "agent_0007"
down_revision = "agent_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE agent.conversations (
        id uuid PRIMARY KEY, team_id uuid NOT NULL, user_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,team_id,user_id)
    )""")
    op.execute("CREATE INDEX conversations_owner_idx ON agent.conversations(team_id,user_id,created_at DESC)")
    op.execute("""CREATE TABLE agent.conversation_turns (
        id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
        ordinal integer NOT NULL CHECK (ordinal>0), run_id uuid NOT NULL UNIQUE REFERENCES agent.agent_runs(id),
        user_text text NOT NULL, idempotency_key text NOT NULL, request_digest bytea NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,ordinal),
        UNIQUE(conversation_id,idempotency_key)
    )""")
    op.execute("CREATE INDEX turns_conversation_idx ON agent.conversation_turns(conversation_id,ordinal DESC)")
    op.execute("""CREATE TABLE agent.conversation_summaries (
        id uuid PRIMARY KEY, conversation_id uuid NOT NULL REFERENCES agent.conversations(id),
        version integer NOT NULL, through_ordinal integer NOT NULL, content text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(conversation_id,version),
        UNIQUE(conversation_id,through_ordinal)
    )""")
    op.execute("""CREATE TABLE agent.personal_facts (
        id uuid PRIMARY KEY, team_id uuid NOT NULL, user_id uuid NOT NULL,
        source_turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id),
        statement text NOT NULL, source_quote text NOT NULL, subject text NOT NULL,
        subject_quote text NOT NULL, subject_turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id),
        stated_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        embedding vector, embedding_model text,
        UNIQUE(source_turn_id,statement,source_quote)
    )""")
    op.execute("CREATE INDEX personal_facts_owner_idx ON agent.personal_facts(team_id,user_id,stated_at DESC)")
    op.execute("CREATE INDEX personal_facts_subject_idx ON agent.personal_facts(team_id,user_id,subject,stated_at DESC)")
    op.execute("""CREATE TABLE agent.memory_jobs (
        id uuid PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('extract','summary')),
        turn_id uuid NOT NULL REFERENCES agent.conversation_turns(id),
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','failed')),
        attempts integer NOT NULL DEFAULT 0, lease_token uuid, leased_until timestamptz,
        error_code text, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(kind,turn_id)
    )""")
    op.execute("CREATE INDEX memory_jobs_claim_idx ON agent.memory_jobs(created_at) WHERE status IN ('pending','running')")
    op.execute("""CREATE TABLE agent.run_memory_snapshots (
        run_id uuid PRIMARY KEY REFERENCES agent.agent_runs(id),
        snapshot jsonb NOT NULL, captured_at timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade():
    for name in ("run_memory_snapshots", "memory_jobs", "personal_facts", "conversation_summaries", "conversation_turns", "conversations"):
        op.execute(f"DROP TABLE agent.{name}")
