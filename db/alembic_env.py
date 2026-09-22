from alembic import context
from sqlalchemy import create_engine, pool, text


def run_migrations() -> None:
    config = context.config
    schema = config.get_main_option("version_table_schema")
    engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        connection.commit()
        context.configure(connection=connection, version_table_schema=schema, include_schemas=True)
        with context.begin_transaction():
            context.run_migrations()
