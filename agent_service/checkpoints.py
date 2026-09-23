"""Initialize LangGraph checkpoint tables explicitly; API/Worker never migrate."""

import os

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def with_agent_search_path(dsn: str) -> str:
    values = conninfo_to_dict(dsn)
    existing = values.get("options", "")
    values["options"] = (existing + " -c search_path=agent").strip()
    return make_conninfo(**values)


def main() -> None:
    with PostgresSaver.from_conn_string(with_agent_search_path(os.environ["MIGRATION_DATABASE_URL"])) as saver:
        saver.setup()


if __name__ == "__main__":
    main()
