"""One PID/loop owns all resources; only top-level entry points force exit."""
import asyncio
import os
from contextlib import asynccontextmanager

from execution.resources import WorkRegistry
from execution.infra.database import AsyncDatabase
from execution.infra.executors import BoundedExecutor
from execution.infra.http import AsyncHttp
from execution.infra.types import OperationDeadline, ResourceClosed, ResourceConfig


class RuntimeResources:
    def __init__(self, role, *, config=None, http_transports=None, on_exit=None):
        if role not in ('agent-api', 'rag-api', 'agent-worker', 'rag-worker'):
            raise ValueError('Unknown runtime role')
        self.pid, self.loop = os.getpid(), asyncio.get_running_loop()
        self.role, self.config = role, config or ResourceConfig.from_env()
        self.state = 'NEW'
        self.exit_reason = None
        self.exit_requested = asyncio.Event()
        self.on_exit = on_exit
        self.registry = WorkRegistry()
        self.stopped_attempts = set()
        self.active = {}
        self.cleanups = set()
        self._close_task = None
        self._closing_deadline = None
        self.db = self.http = self.io = self.cpu = None
        self._http_transports = http_transports

    @property
    def accepting(self):
        return self.state == 'RUNNING' and self.exit_reason is None

    def affinity(self):
        if os.getpid() != self.pid:
            raise ResourceClosed('Runtime belongs to another process')
        if asyncio.get_running_loop() is not self.loop:
            raise ResourceClosed('Runtime belongs to another event loop')

    def check(self):
        self.affinity()
        if not self.accepting:
            raise ResourceClosed('Runtime is not accepting work')

    def check_cleanup(self, profile):
        self.affinity()
        if profile != 'control' or self.state not in ('RUNNING', 'QUIESCING'):
            raise ResourceClosed('Only bounded control cleanup is admitted')
        if self._closing_deadline and self._closing_deadline.remaining() <= 0:
            raise ResourceClosed('Shutdown deadline expired')

    def begin_shutdown(self):
        self.affinity()
        if self._closing_deadline is None:
            self._closing_deadline = OperationDeadline.after(self.config.exit_grace)
        self.quiesce()
        return self._closing_deadline

    async def __aenter__(self):
        self.affinity()
        if self.state != 'NEW':
            raise ResourceClosed('Runtime cannot be restarted')
        self.state = 'STARTING'
        try:
            self.db = AsyncDatabase(self)
            await self.db.open()
            self.http = AsyncHttp(self, self._http_transports)
            self.io = BoundedExecutor(self, 'io')
            self.cpu = BoundedExecutor(self, 'cpu')
            self.state = 'RUNNING'
            from execution.tokens import configuration
            self.token_settings=await self.io.run(configuration,deadline=OperationDeadline.after(30))
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *_):
        await self.aclose()

    def cancel_attempt(self, owner):
        self.affinity()
        self.stopped_attempts.add(owner.attempt)
        for executor in (self.io, self.cpu):
            if executor:
                executor.cancel_attempt(owner)

    def quiesce(self):
        self.affinity()
        if self.state in ('RUNNING', 'STARTING'):
            self.state = 'QUIESCING'
        for executor in (self.io, self.cpu):
            if executor:
                executor.quiesce()
        if self.http:
            self.http.quiesce()
        if hasattr(self, 'tool_gate'):
            self.tool_gate.wake()
            for gate in self.run_tool_gates.values(): gate.wake()
        # Cancel DB capacity waiters promptly without interrupting borrowed work.
        for task, stages in list(self.active.items()):
            if 'db-wait' in stages:
                task.cancel()

    def request_exit(self, reason):
        if self.exit_reason is None:
            self.exit_reason = reason
            self.exit_requested.set()
            self.quiesce()
            if self.on_exit:
                self.on_exit(self)

    @asynccontextmanager
    async def operation(self, stage='active', *, cleanup=False):
        self.check_cleanup('control') if cleanup else self.check()
        task = asyncio.current_task()
        self.active.setdefault(task, []).append(stage)
        try:
            yield
        finally:
            stages = self.active[task]
            stages.pop()
            if not stages:
                self.active.pop(task)

    async def cleanup(self, awaitable):
        """Finish cleanup before releasing a borrow, even on repeated cancellation."""
        task = self.loop.create_task(awaitable)
        self.cleanups.add(task)
        def consume(done):
            self.cleanups.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(consume)
        deadline = self._closing_deadline or OperationDeadline.after(self.config.exit_grace)
        cancelled = False
        while not task.done():
            if self._closing_deadline:
                deadline = OperationDeadline(min(deadline.when, self._closing_deadline.when))
            if deadline.remaining() <= 0:
                task.cancel()
                self.request_exit('resource_cleanup_deadline_expired')
                raise TimeoutError('Resource cleanup deadline expired')
            try:
                await asyncio.wait({task}, timeout=deadline.remaining())
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def aclose(self):
        self.affinity()
        if self._close_task is None:
            self.begin_shutdown()
            self._close_task = self.loop.create_task(self._close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        report = self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError
        return report

    async def _close(self):
        self.state = 'CLOSING'
        deadline = self._closing_deadline
        failures = []
        try:
            async with asyncio.timeout_at(deadline.when):
                for executor in (self.cpu, self.io):
                    if executor:
                        await executor.aclose(deadline)
                # Cancel borrowers so DB rolls back and streams close before pools.
                tasks = [task for task in self.active if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=deadline.remaining())
                    if pending:
                        raise TimeoutError('Borrower cleanup deadline expired')
                while self.cleanups:
                    done, pending = await asyncio.wait(tuple(self.cleanups), timeout=deadline.remaining())
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                    if pending:
                        raise TimeoutError('Resource cleanup deadline expired')
                if self.http:
                    await self.http.aclose()
                if self.db:
                    await self.db.aclose(deadline)
        except Exception as exc:
            failures.append(type(exc).__name__)
            self.request_exit('resource_close_deadline_or_failure')
            if self.cpu and self.cpu.pool:
                self.cpu.pool.emergency_kill()
        finally:
            self.state = 'CLOSED'
        return {'state': self.state, 'exit_reason': self.exit_reason, 'failures': failures,
                'unfinished': self.registry.occupied, 'cleanup_tasks': len(self.cleanups), 'borrowers': len(self.active),
                'shutdown_complete': self.shutdown_complete}

    @property
    def shutdown_complete(self):
        return (self.state == 'CLOSED' and not self.registry.occupied
                and not self.active and not self.cleanups
                and all(executor is None or executor.retired for executor in (self.io, self.cpu))
                and (self.http is None or all(client.is_closed for client in self.http.clients.values()))
                and (self.db is None or all(pool.closed for pool in self.db.pools.values())))

    def snapshot(self):
        self.affinity()
        return {'state': self.state, 'accepting': self.accepting, 'exit_reason': self.exit_reason,
                'db': self.db.snapshot() if self.db else {},
                'http': self.http.snapshot() if self.http else {},
                'io': self.io.snapshot() if self.io else {},
                'cpu': self.cpu.snapshot() if self.cpu else {}}
