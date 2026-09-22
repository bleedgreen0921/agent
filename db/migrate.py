"""Run one independent migration chain at a time, in dependency order."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]


def upgrade(chain: str) -> None:
    if chain not in {"identity", "rag", "agent"}:
        raise ValueError(chain)
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / ("rag_service" if chain == "rag" else "agent_service" if chain == "agent" else "identity") / "migrations"))
    cfg.set_main_option("sqlalchemy.url", os.environ["MIGRATION_DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1))
    cfg.set_main_option("version_table_schema", chain)
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    for item in ("identity", "rag", "agent"):
        upgrade(item)
