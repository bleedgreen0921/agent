"""Retry whole DB operations only after the resource layer confirms rollback."""
import asyncio
import random
from functools import wraps
import psycopg
from contextvars import ContextVar

_retry_scope = ContextVar('database_retry_scope', default=False)


class TransactionRolledBack(psycopg.OperationalError):
    def __init__(self, sqlstate):
        super().__init__('Database transaction was rolled back: ' + sqlstate)
        self.rollback_sqlstate = sqlstate


async def backoff_wait(seconds):
    await asyncio.sleep(seconds)


async def rolled_back_retry(operation, *, max_attempts=3, profile='normal'):
    if _retry_scope.get():
        # Nested complete operations share the outer policy and budget; they
        # cannot multiply three attempts into nine.
        return await operation()
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('Transaction retry permits one to three attempts')
    from execution.persistence import deadline, resources
    limit = deadline(resources().db.timeouts[profile][0] / 1000)
    from execution.persistence import _operation_deadline
    bound = _operation_deadline.set(limit)
    scoped = _retry_scope.set(True)
    try:
        for attempt in range(max_attempts):
            try:
                async with asyncio.timeout_at(limit.when):
                    return await operation()
            except TransactionRolledBack:
                if attempt + 1 >= max_attempts:
                    raise
                # Sleep only after connection/locks were returned. The original
                # call, attempt and shutdown deadline remain in force.
                delay = random.uniform(0, min(.25, .025 * (2 ** attempt)))
                if limit.remaining() <= delay:
                    raise
                async with asyncio.timeout_at(limit.when):
                    await backoff_wait(delay)
    finally:
        _retry_scope.reset(scoped)
        _operation_deadline.reset(bound)


def retry_transaction(fn=None, *, profile='normal'):
    """For SQL-only complete operations, never individual SQL or external calls."""
    if fn is None:
        return lambda function: retry_transaction(function, profile=profile)
    @wraps(fn)
    async def invoke(*args, **kwargs):
        return await rolled_back_retry(lambda: fn(*args, **kwargs), profile=profile)
    return invoke
