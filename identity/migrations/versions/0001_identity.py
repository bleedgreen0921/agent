from alembic import op

revision = "identity_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS identity")
    op.execute("""CREATE TABLE identity.teams (
        id uuid PRIMARY KEY, name text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE identity.credentials (
        key_id text PRIMARY KEY, kind text NOT NULL CHECK (kind IN ('admin', 'team')),
        team_id uuid REFERENCES identity.teams(id), digest bytea NOT NULL,
        expires_at timestamptz, revoked_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK ((kind = 'admin' AND team_id IS NULL) OR (kind = 'team' AND team_id IS NOT NULL))
    )""")
    op.execute("CREATE INDEX credentials_team_id_idx ON identity.credentials(team_id)")


def downgrade():
    op.execute("DROP TABLE identity.credentials")
    op.execute("DROP TABLE identity.teams")
