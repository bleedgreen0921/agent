"""Initialize only empty/personal databases, execution → rag → agent."""
import os
from pathlib import Path
import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
CHAINS = {'execution': ('execution', 'execution_0001'), 'rag': ('rag_service', 'rag_personal_0001'),
          'agent': ('agent_service', 'agent_personal_0001')}


def validate_database(conn) -> None:
    if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname='identity'").fetchone():
        raise RuntimeError('Historical database rejected without modification; use a new personal database and files directory')
    relations = conn.execute("""SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','S','f') AND n.nspname NOT LIKE 'pg_%'
        AND n.nspname<>'information_schema'""").fetchall()
    rows = conn.execute("""SELECT table_schema,table_name,column_name FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog','information_schema')""").fetchall()
    for schema, table, column in rows:
        if schema == 'identity' or column in {'team_id','user_id','caller_key_id','key_id','visibility'} or table == 'document_access':
            raise RuntimeError('Historical database rejected without modification; use a new personal database and files directory')
    revisions = {}
    for chain, (_, expected) in CHAINS.items():
        if conn.execute('SELECT to_regclass(%s)', (chain+'.alembic_version',)).fetchone()[0]:
            versions = conn.execute(f'SELECT version_num FROM {chain}.alembic_version').fetchall()
            cfg = Config()
            cfg.set_main_option('script_location', str(ROOT / CHAINS[chain][0] / 'migrations'))
            known = {revision.revision for revision in ScriptDirectory.from_config(cfg).walk_revisions()}
            if expected not in known or not versions or any(version not in known for (version,) in versions):
                raise RuntimeError('Unsupported or historical revision; the database was not modified')
            revisions[chain] = expected
    business = {(s,t) for s,t in relations if s in {'execution','agent','rag'}}
    if any(s not in revisions for s,t in business):
        raise RuntimeError('Unrecognized nonempty database; initialization refused without modification')
    # Unrelated application data is also not an empty database.
    if any(s not in {'execution','agent','rag'} for s,t in relations):
        raise RuntimeError('Nonempty database is not a personal instance')


def upgrade(chain: str) -> None:
    if chain not in CHAINS:
        raise ValueError(chain)
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as conn:
        validate_database(conn)
        prior = list(CHAINS)[:list(CHAINS).index(chain)]
        if any(not conn.execute('SELECT to_regclass(%s)', (p+'.alembic_version',)).fetchone()[0] for p in prior):
            raise RuntimeError('Migration dependency missing; initialize execution → rag → agent')
    cfg = Config()
    cfg.set_main_option('script_location', str(ROOT / CHAINS[chain][0] / 'migrations'))
    cfg.set_main_option('sqlalchemy.url', os.environ['MIGRATION_DATABASE_URL'].replace('postgresql://', 'postgresql+psycopg://', 1).replace('%','%%'))
    cfg.set_main_option('version_table_schema', chain)
    command.upgrade(cfg, 'head')

if __name__ == '__main__':
    for chain in CHAINS:
        upgrade(chain)
