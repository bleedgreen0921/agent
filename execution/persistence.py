"""Resource-bound async persistence. Connections never cross operation boundaries."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from execution.infra.types import OperationDeadline

_resources = ContextVar('persistent_resources', default=None)
_operation_deadline = ContextVar('persistent_deadline', default=None)
_cleanup = ContextVar('persistent_cleanup', default=False)
_call_deadline = ContextVar('external_call_deadline', default=None)


@contextmanager
def call_scope(limit):
    token = _call_deadline.set(limit)
    try:
        yield
    finally:
        _call_deadline.reset(token)


def current_call_deadline():
    return _call_deadline.get()


@dataclass(frozen=True)
class Applied:
    value: Any = None
    def __bool__(self): return True


@dataclass(frozen=True)
class AlreadyApplied(Applied):
    pass


@dataclass(frozen=True)
class Rejected:
    reason: str
    def __bool__(self): return False


class PersistenceOutcomeUnknown(RuntimeError):
    """Commit may have succeeded. Never retry an external operation on this signal."""
    def __init__(self, message='Transaction outcome unknown', *, operation=None, resolution=None):
        super().__init__(message)
        self.operation = operation
        self.resolution = resolution


def resources():
    value = _resources.get()
    if value is None:
        raise RuntimeError('Persistence requires an explicitly bound RuntimeResources')
    value.affinity()
    return value


@contextmanager
def bind(value, *, deadline=None, cleanup=False):
    tokens = (_resources.set(value), _operation_deadline.set(deadline), _cleanup.set(cleanup))
    try:
        yield value
    finally:
        _cleanup.reset(tokens[2]); _operation_deadline.reset(tokens[1]); _resources.reset(tokens[0])


def deadline(seconds=30):
    limit = OperationDeadline.after(seconds)
    limits = [limit.when]
    limits.extend(value.when for value in (_operation_deadline.get(), _call_deadline.get()) if value is not None)
    return OperationDeadline(min(limits))


@asynccontextmanager
async def transaction(env=None, *, profile='normal', readonly=False, isolation=None):
    runtime = resources()
    if env and env != ('AGENT_DATABASE_URL' if runtime.role.startswith('agent') else 'RAG_DATABASE_URL'):
        raise PermissionError('Database does not belong to this runtime')
    limit = deadline(runtime.db.timeouts[profile][0]/1000)
    from execution.progress import activity
    from contextlib import nullcontext
    phase = activity('database:' + profile, limit) if profile != 'control' else nullcontext()
    async with phase:
        async with runtime.db.transaction(profile, limit, readonly=readonly, cleanup=_cleanup.get(), isolation=isolation) as conn:
            yield conn



class ResourceBinding:
    """ASGI request scope; ContextVars isolate simultaneous requests and attempts."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        runtime = scope['app'].state.resources
        with bind(runtime):
            await self.app(scope, receive, send)


@contextmanager
def using(runtime):
    runtime.affinity()
    token=_resources.set(runtime)
    try:yield runtime
    finally:_resources.reset(token)
