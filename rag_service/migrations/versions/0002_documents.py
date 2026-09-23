from alembic import op

revision = "rag_0002"
down_revision = "rag_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE rag.documents ADD COLUMN active_version_id uuid")
    op.execute("""CREATE TABLE rag.document_access (
        document_id uuid NOT NULL REFERENCES rag.documents(id) ON DELETE CASCADE,
        team_id uuid NOT NULL,
        PRIMARY KEY(document_id, team_id)
    )""")
    op.execute("""CREATE TABLE rag.document_versions (
        id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES rag.documents(id),
        version_number integer NOT NULL, file_sha256 bytea NOT NULL,
        file_path text NOT NULL, media_type text NOT NULL,
        status text NOT NULL CHECK (status IN ('pending','processing','active','superseded','failed')),
        error_code text, warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(), activated_at timestamptz,
        UNIQUE(document_id, version_number)
    )""")
    op.execute("ALTER TABLE rag.documents ADD CONSTRAINT documents_active_version_fk FOREIGN KEY (active_version_id) REFERENCES rag.document_versions(id)")
    op.execute("CREATE UNIQUE INDEX one_pending_version ON rag.document_versions(document_id) WHERE status IN ('pending','processing')")
    op.execute("""CREATE TABLE rag.upload_keys (
        caller_key_id text NOT NULL, scope text NOT NULL,
        document_id uuid NOT NULL REFERENCES rag.documents(id), key text NOT NULL,
        request_digest bytea NOT NULL, version_id uuid NOT NULL REFERENCES rag.document_versions(id),
        PRIMARY KEY(caller_key_id, scope, key)
    )""")
    op.execute("""CREATE TABLE rag.processing_jobs (
        id uuid PRIMARY KEY, version_id uuid NOT NULL UNIQUE REFERENCES rag.document_versions(id),
        status text NOT NULL CHECK (status IN ('queued','running','retry','done','failed')),
        attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        lease_token uuid, leased_until timestamptz, heartbeat_at timestamptz,
        error_code text, updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX processing_jobs_claim_idx ON rag.processing_jobs(available_at) WHERE status IN ('queued','retry','running')")
    op.execute("""CREATE TABLE rag.chunks (
        id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES rag.documents(id),
        version_id uuid NOT NULL REFERENCES rag.document_versions(id),
        ordinal integer NOT NULL, content text NOT NULL, heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
        source_locator jsonb NOT NULL, table_html text,
        search_text text NOT NULL,
        search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
        UNIQUE(version_id, ordinal)
    )""")
    op.execute("CREATE INDEX chunks_search_idx ON rag.chunks USING gin(search_vector)")
    op.execute("CREATE INDEX chunks_version_idx ON rag.chunks(version_id)")
    op.execute("""CREATE TABLE rag.index_revisions (
        id uuid PRIMARY KEY, model text NOT NULL, dimensions integer NOT NULL CHECK (dimensions > 0),
        document_template text NOT NULL, query_template text NOT NULL,
        state text NOT NULL CHECK (state IN ('active','building','superseded')),
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE UNIQUE INDEX one_building_revision ON rag.index_revisions(state) WHERE state='building'")
    op.execute("""CREATE TABLE rag.index_state (
        singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        active_revision_id uuid NOT NULL REFERENCES rag.index_revisions(id)
    )""")
    op.execute("""CREATE TABLE rag.embeddings (
        chunk_id uuid NOT NULL REFERENCES rag.chunks(id) ON DELETE CASCADE,
        revision_id uuid NOT NULL REFERENCES rag.index_revisions(id),
        embedding vector NOT NULL, PRIMARY KEY(chunk_id, revision_id)
    )""")
    op.execute("CREATE INDEX embeddings_revision_idx ON rag.embeddings(revision_id)")
    op.execute("""CREATE TABLE rag.retrieval_audit (
        id uuid PRIMARY KEY, request_id text NOT NULL, run_id text NOT NULL,
        tool_call_id text NOT NULL, team_id uuid NOT NULL, evidence_id uuid,
        revision_id uuid, status text NOT NULL, degradations jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)
        VALUES ('00000000-0000-0000-0000-000000000001','Qwen3-Embedding-0.6B',1024,'title-content-v1','qwen-retrieval-v2','active')""")
    op.execute("INSERT INTO rag.index_state(singleton,active_revision_id) VALUES (true,'00000000-0000-0000-0000-000000000001')")


def downgrade():
    for table in ("retrieval_audit", "embeddings", "index_state", "index_revisions", "chunks", "processing_jobs", "upload_keys"):
        op.execute(f"DROP TABLE rag.{table}")
    op.execute("ALTER TABLE rag.documents DROP CONSTRAINT documents_active_version_fk")
    op.execute("DROP TABLE rag.document_versions")
    op.execute("DROP TABLE rag.document_access")
    op.execute("ALTER TABLE rag.documents DROP COLUMN active_version_id")
