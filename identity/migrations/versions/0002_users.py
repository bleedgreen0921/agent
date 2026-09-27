from alembic import op

revision = "identity_0002"
down_revision = "identity_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE identity.users (
        id uuid PRIMARY KEY, team_id uuid NOT NULL REFERENCES identity.teams(id),
        name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(id, team_id)
    )""")
    op.execute("CREATE INDEX users_team_idx ON identity.users(team_id, created_at)")
    op.execute("ALTER TABLE identity.credentials ADD COLUMN user_id uuid")
    op.execute("ALTER TABLE identity.credentials DROP CONSTRAINT credentials_kind_check")
    op.execute("ALTER TABLE identity.credentials DROP CONSTRAINT credentials_check")
    op.execute("ALTER TABLE identity.credentials ADD CONSTRAINT credentials_kind_check CHECK (kind IN ('admin','team','user'))")
    op.execute("""ALTER TABLE identity.credentials ADD CONSTRAINT credentials_scope_check CHECK (
        (kind='admin' AND team_id IS NULL AND user_id IS NULL) OR
        (kind='team' AND team_id IS NOT NULL AND user_id IS NULL) OR
        (kind='user' AND team_id IS NOT NULL AND user_id IS NOT NULL))""")
    op.execute("ALTER TABLE identity.credentials ADD CONSTRAINT credentials_user_team_fk FOREIGN KEY (user_id,team_id) REFERENCES identity.users(id,team_id)")
    op.execute("CREATE INDEX credentials_user_idx ON identity.credentials(user_id)")


def downgrade():
    op.execute("DROP INDEX identity.credentials_user_idx")
    op.execute("ALTER TABLE identity.credentials DROP CONSTRAINT credentials_user_team_fk")
    op.execute("ALTER TABLE identity.credentials DROP CONSTRAINT credentials_scope_check")
    op.execute("ALTER TABLE identity.credentials ADD CONSTRAINT credentials_check CHECK ((kind='admin' AND team_id IS NULL) OR (kind='team' AND team_id IS NOT NULL))")
    op.execute("ALTER TABLE identity.credentials DROP CONSTRAINT credentials_kind_check")
    op.execute("ALTER TABLE identity.credentials ADD CONSTRAINT credentials_kind_check CHECK (kind IN ('admin','team'))")
    op.execute("ALTER TABLE identity.credentials DROP COLUMN user_id")
    op.execute("DROP TABLE identity.users")
