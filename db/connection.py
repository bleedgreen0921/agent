import os
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


TIMEOUT_PROFILES = {
    "normal": ("DB_STATEMENT_TIMEOUT_SECONDS", 120, "DB_LOCK_TIMEOUT_SECONDS", 30),
    "control": ("DB_CONTROL_STATEMENT_TIMEOUT_SECONDS", 15, "DB_CONTROL_LOCK_TIMEOUT_SECONDS", 5),
    "bulk": ("DB_BULK_STATEMENT_TIMEOUT_SECONDS", 3600, "DB_BULK_LOCK_TIMEOUT_SECONDS", 60),
}


def positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def bounded_conninfo(dsn: str, profile: str = "normal") -> tuple[str, int]:
    statement_name, statement_default, lock_name, lock_default = TIMEOUT_PROFILES[profile]
    statement = positive_int_env(statement_name, statement_default)
    lock = positive_int_env(lock_name, lock_default)
    for name, value in ((statement_name, statement), (lock_name, lock)):
        if value > 2_147_483:
            raise ValueError(f"{name} exceeds the PostgreSQL timeout limit")
    connect_timeout = positive_int_env("DB_CONNECT_TIMEOUT_SECONDS", 10)
    values = conninfo_to_dict(dsn)
    existing = values.get("options", "")
    values["options"] = (existing + f" -c statement_timeout={statement * 1000} -c lock_timeout={lock * 1000}").strip()
    return make_conninfo(**values), connect_timeout


@contextmanager
def connect(env_name: str, *, profile: str = "normal"):
    dsn, timeout = bounded_conninfo(os.environ[env_name], profile)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=timeout) as conn:
        yield conn
