"""Three process-local async pools. Business transactions own one connection."""
import asyncio
import os
import psycopg
from contextlib import asynccontextmanager

from psycopg.rows import dict_row
from psycopg.pq import TransactionStatus
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests

from db.connection import bounded_conninfo
from execution.infra.types import CapacityTimeout, ResourceUnavailable


class AsyncDatabase:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pools = {}
        self.timeouts = {}
        config = runtime.config
        env = 'AGENT_DATABASE_URL' if runtime.role.startswith('agent') else 'RAG_DATABASE_URL'
        for profile in ('control', 'normal', 'bulk'):
            dsn, connect_timeout = bounded_conninfo(os.environ[env], profile)
            # Capture the fixed settings once; no env reads during operations.
            options = conninfo_to_dict(dsn)['options'].split()
            self.timeouts[profile] = tuple(int(next(v.split('=')[1] for v in reversed(options) if v.startswith(name + '=')))
                                           for name in ('statement_timeout', 'lock_timeout'))
            self.pools[profile] = AsyncConnectionPool(
                dsn, open=False, min_size=0, max_size=getattr(config, 'db_' + profile),
                max_waiting=config.db_waiting, timeout=config.db_acquire,
                kwargs={'autocommit': True, 'row_factory': dict_row, 'connect_timeout': connect_timeout},
                name=f'{runtime.role}-{profile}')

    async def open(self):
        for pool in self.pools.values():
            await pool.open(wait=False)

    @asynccontextmanager
    async def transaction(self, profile, deadline, readonly=False, *, cleanup=False, isolation=None):
        if isolation not in (None, 'read committed', 'repeatable read', 'serializable'):
            raise ValueError('Unsupported transaction isolation')
        self.runtime.check_cleanup(profile) if cleanup else self.runtime.check()
        pool = self.pools[profile]
        if deadline.remaining() <= 0:
            raise CapacityTimeout('Database operation deadline expired')
        conn = None
        async with self.runtime.operation('db-wait', cleanup=cleanup):
            try:
                async with asyncio.timeout_at(deadline.when):
                    try:
                        from execution.timing import span
                        with span('db_pool_wait','wait'):
                            conn = await pool.getconn(timeout=min(self.runtime.config.db_acquire, deadline.remaining()))
                    except (PoolTimeout, TooManyRequests) as exc:
                        raise CapacityTimeout('Database connection admission unavailable') from exc
                    self.runtime.check_cleanup(profile) if cleanup else self.runtime.check()
                    self.runtime.active[asyncio.current_task()][-1] = 'db-active'
                    body_finished = False
                    try:
                        async with conn.transaction():
                            # Isolation must be selected before set_config's SELECT
                            # establishes the transaction's first data snapshot.
                            if isolation is not None:
                                await conn.execute('SET TRANSACTION ISOLATION LEVEL ' + isolation)
                            if readonly:
                                await conn.execute('SET TRANSACTION READ ONLY')
                            ms = max(1, int(deadline.remaining() * 1000))
                            statement, lock = self.timeouts[profile]
                            await conn.execute("SELECT set_config('statement_timeout',%s,true), set_config('lock_timeout',%s,true)",
                                               (str(min(statement, ms)), str(min(lock, ms))))
                            yield conn
                            body_finished = True
                    except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected) as exc:
                        if conn.info.transaction_status == TransactionStatus.IDLE:
                            from execution.retry import TransactionRolledBack
                            raise TransactionRolledBack(exc.sqlstate) from exc
                        # No acknowledged rollback: it is never replayable.
                        from execution.persistence import PersistenceOutcomeUnknown
                        raise PersistenceOutcomeUnknown('Transaction rollback was not acknowledged') from exc
                    except (asyncio.CancelledError, psycopg.OperationalError):
                        if body_finished and not readonly:
                            from execution.persistence import PersistenceOutcomeUnknown
                            raise PersistenceOutcomeUnknown('Transaction commit was not acknowledged') from None
                        raise
            finally:
                if conn is not None:
                    async def give_back():
                        # Uncertain cancellation/commit cleanup: discard instead of reusing.
                        if conn.info.transaction_status != TransactionStatus.IDLE:
                            await conn.close()
                        await pool.putconn(conn)
                    await self.runtime.cleanup(give_back())

    async def aclose(self, deadline):
        for pool in reversed(list(self.pools.values())):
            await pool.close(timeout=deadline.remaining())

    def snapshot(self):
        safe = ('pool_size', 'pool_available', 'requests_waiting')
        return {name: {key: pool.get_stats().get(key, 0) for key in safe} for name, pool in self.pools.items()}
