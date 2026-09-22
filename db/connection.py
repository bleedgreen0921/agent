import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


@contextmanager
def connect(env_name: str):
    dsn = os.environ[env_name]
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn
