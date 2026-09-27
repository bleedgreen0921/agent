from alembic import op

revision = "rag_0005"
down_revision = "rag_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE rag.processing_jobs ADD COLUMN deadline_at timestamptz")
    op.execute("""UPDATE rag.processing_jobs SET deadline_at=now()+interval '4 hours'
        WHERE attempts>0 AND status IN ('running','retry','queued')""")
    op.execute("""CREATE TABLE rag.worker_instances (
        instance_id uuid PRIMARY KEY,
        started_at timestamptz NOT NULL DEFAULT now(),
        heartbeat_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX rag_worker_heartbeat_idx ON rag.worker_instances(heartbeat_at)")


def downgrade():
    op.execute("DROP TABLE rag.worker_instances")
    op.execute("ALTER TABLE rag.processing_jobs DROP COLUMN deadline_at")
