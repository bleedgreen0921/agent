from alembic import op

revision = "agent_0008"
down_revision = "agent_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE agent.worker_instances (
        instance_id uuid PRIMARY KEY,
        started_at timestamptz NOT NULL DEFAULT now(),
        heartbeat_at timestamptz NOT NULL DEFAULT now(),
        memory_child_alive boolean NOT NULL DEFAULT false
    )""")
    op.execute("CREATE INDEX agent_worker_heartbeat_idx ON agent.worker_instances(heartbeat_at)")


def downgrade():
    op.execute("DROP TABLE agent.worker_instances")
