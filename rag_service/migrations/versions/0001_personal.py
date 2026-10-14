"""Fresh personal instance baseline. Historical databases are deliberately unsupported."""
from alembic import op

revision = 'rag_personal_0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute('CREATE SCHEMA IF NOT EXISTS rag')
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute("""
        CREATE TABLE rag.documents (
                id uuid PRIMARY KEY, title text NOT NULL,
                deleted_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute('ALTER TABLE rag.documents ADD COLUMN active_version_id uuid')
    op.execute("""
        CREATE TABLE rag.document_versions (
                id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES rag.documents(id),
                version_number integer NOT NULL, file_sha256 bytea NOT NULL,
                file_path text NOT NULL, media_type text NOT NULL,
                status text NOT NULL CHECK (status IN ('pending','processing','active','superseded','failed')),
                error_code text, warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now(), activated_at timestamptz,
                UNIQUE(document_id, version_number)
            )
    """)
    op.execute("""
        CREATE TABLE rag.processing_jobs (
         id uuid PRIMARY KEY, task_kind text NOT NULL DEFAULT 'rag_document' CHECK(task_kind='rag_document'),
         version_id uuid NOT NULL UNIQUE REFERENCES rag.document_versions(id),
         FOREIGN KEY(id,task_kind) REFERENCES execution.tasks(id,kind))
    """)
    op.execute('ALTER TABLE rag.documents ADD CONSTRAINT documents_active_version_fk FOREIGN KEY (active_version_id) REFERENCES rag.document_versions(id)')
    op.execute("CREATE UNIQUE INDEX one_pending_version ON rag.document_versions(document_id) WHERE status IN ('pending','processing')")
    op.execute("""
        CREATE TABLE rag.upload_keys (
                scope text NOT NULL,
                document_id uuid NOT NULL REFERENCES rag.documents(id), key text NOT NULL,
                request_digest bytea NOT NULL, version_id uuid NOT NULL REFERENCES rag.document_versions(id),
                PRIMARY KEY(scope, key)
            )
    """)
    op.execute("""
        CREATE TABLE rag.chunks (
                id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES rag.documents(id),
                version_id uuid NOT NULL REFERENCES rag.document_versions(id),
                ordinal integer NOT NULL, content text NOT NULL, heading_path jsonb NOT NULL DEFAULT '[]'::jsonb,
                source_locator jsonb NOT NULL, table_html text,
                search_text text NOT NULL,
                search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
                UNIQUE(version_id, ordinal)
            )
    """)
    op.execute('CREATE INDEX chunks_search_idx ON rag.chunks USING gin(search_vector)')
    op.execute('CREATE INDEX chunks_version_idx ON rag.chunks(version_id)')
    op.execute("""
        CREATE TABLE rag.index_revisions (
                id uuid PRIMARY KEY, model text NOT NULL, dimensions integer NOT NULL CHECK (dimensions > 0),
                document_template text NOT NULL, query_template text NOT NULL,
                state text NOT NULL CHECK (state IN ('active','building','superseded')),
                created_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("CREATE UNIQUE INDEX one_building_revision ON rag.index_revisions(state) WHERE state='building'")
    op.execute("""
        CREATE TABLE rag.index_state (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                active_revision_id uuid NOT NULL REFERENCES rag.index_revisions(id)
            )
    """)
    op.execute("""
        CREATE TABLE rag.embeddings (
                chunk_id uuid NOT NULL REFERENCES rag.chunks(id) ON DELETE CASCADE,
                revision_id uuid NOT NULL REFERENCES rag.index_revisions(id),
                embedding vector NOT NULL, PRIMARY KEY(chunk_id, revision_id)
            )
    """)
    op.execute('CREATE INDEX embeddings_revision_idx ON rag.embeddings(revision_id)')
    op.execute("""
        CREATE TABLE rag.retrieval_audit (
                id uuid PRIMARY KEY, request_id text NOT NULL, run_id text NOT NULL,
                tool_call_id text NOT NULL, evidence_id uuid,
                revision_id uuid, status text NOT NULL, degradations jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute("""
        INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)
                VALUES ('00000000-0000-0000-0000-000000000001','Qwen3-Embedding-0.6B',1024,'title-content-v1','qwen-retrieval-v2','active')
    """)
    op.execute("INSERT INTO rag.index_state(singleton,active_revision_id) VALUES (true,'00000000-0000-0000-0000-000000000001')")
    op.execute("""
        ALTER TABLE rag.retrieval_audit
                ADD COLUMN operation text,
                ADD COLUMN retrieval_id text,
                ADD COLUMN query_sha256 text,
                ADD COLUMN rewritten_query_sha256 text,
                ADD COLUMN selected_evidence_ids jsonb,
                ADD COLUMN error_code text
    """)
    op.execute('ALTER TABLE rag.retrieval_audit ALTER COLUMN operation SET NOT NULL')
    op.execute("ALTER TABLE rag.retrieval_audit ADD CONSTRAINT retrieval_audit_operation_check CHECK (operation IN ('search', 'read'))")
    op.execute('CREATE UNIQUE INDEX retrieval_audit_retrieval_id_idx ON rag.retrieval_audit(retrieval_id) WHERE retrieval_id IS NOT NULL')
    op.execute('CREATE INDEX retrieval_audit_run_tool_created_idx ON rag.retrieval_audit(run_id,tool_call_id,created_at)')
    op.execute('ALTER TABLE rag.retrieval_audit ADD COLUMN candidate_trace jsonb')
    op.execute("""
        CREATE TABLE rag.worker_instances (
                instance_id uuid PRIMARY KEY,
                started_at timestamptz NOT NULL DEFAULT now(),
                heartbeat_at timestamptz NOT NULL DEFAULT now()
            )
    """)
    op.execute('CREATE INDEX rag_worker_heartbeat_idx ON rag.worker_instances(heartbeat_at)')
    op.execute("""
        CREATE VIEW rag.processing_job_state WITH (security_invoker=true) AS
        SELECT j.*,CASE WHEN t.status='queued' AND t.attempts>0 THEN 'retry' WHEN t.status='completed' THEN 'done' ELSE t.status END AS status,t.attempts,t.created_at,t.available_at,t.error_code,t.execution_deadline_at AS deadline_at,t.lease_token,t.leased_until
        FROM rag.processing_jobs j
        JOIN execution.tasks t ON t.id=j.id AND t.kind=j.task_kind
    """)

def downgrade():
    raise RuntimeError("Personal baselines cannot be downgraded; restore the archived database with its matching code")
