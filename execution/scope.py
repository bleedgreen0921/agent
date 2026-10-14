"""One attempt identity, stop signal and monotonic deadline across nested calls."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
import asyncio
from uuid import UUID
from execution import store
from execution.contracts import TaskRef, bounded_timeout
from execution.infra.types import OperationDeadline, WorkOwner
from execution.errors import ExecutionRevoked


@dataclass
class AttemptContext:
    task: TaskRef
    token: UUID
    stopped: object = None
    deadline: OperationDeadline | None = None
    children: set = field(default_factory=set, repr=False)
    root: object = field(default=None, repr=False)
    closing: bool = False


_current = ContextVar('execution_attempt', default=None)
_call_id = ContextVar('execution_call_id', default=None)


@contextmanager
def call_identity(identity):
    key = _call_id.set(identity)
    try: yield
    finally: _call_id.reset(key)


@contextmanager
def attempt(task, token, stop_signal=None, *, deadline=None):
    outer = _current.get()
    if outer is not None:
        if outer.task != task or outer.token != token:
            raise ExecutionRevoked('Nested attempt identity differs from its owner')
        if stop_signal is not None and stop_signal is not outer.stopped:
            raise ExecutionRevoked('Nested attempt cannot replace its stop signal')
        if deadline is not None and (outer.deadline is None or deadline.when != outer.deadline.when):
            raise ExecutionRevoked('Nested attempt cannot replace its deadline')
        yield outer
        return
    value = AttemptContext(task, token, stop_signal, deadline, root=asyncio.current_task())
    key = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(key)


async def call_timeout(configured):
    ref = _current.get()
    if ref is None:
        return configured
    local_authority(ref.task, ref.token)
    row = await store.check(ref.task, ref.token)
    return bounded_timeout(configured, row['execution_deadline_at'], row['observed_at'])


def local_authority(task, token):
    ref = _current.get()
    if ref is None:
        return
    if ref.task != task or ref.token != token:
        raise ExecutionRevoked('Operation does not belong to the current attempt')
    if ref.closing or (ref.stopped is not None and ref.stopped.is_set()):
        raise ExecutionRevoked('Attempt stopped after losing execution authority')
    if ref.deadline is not None and ref.deadline.remaining() <= 0:
        raise ExecutionRevoked('Attempt deadline expired')


def current_attempt():
    return _current.get()


def work_owner(call_id=None):
    ref = current_attempt()
    return WorkOwner(ref.task, ref.token, call_id if call_id is not None else _call_id.get()) if ref else None


async def authorize():
    ref = current_attempt()
    if ref:
        local_authority(ref.task, ref.token)
        return await store.check(ref.task, ref.token)


async def operation_deadline(configured):
    from execution.persistence import deadline
    return deadline(await call_timeout(configured))



def track_call_task():
    ref = current_attempt()
    task = asyncio.current_task()
    if ref is not None and task is not ref.root and task not in ref.children:
        ref.children.add(task)
        task.add_done_callback(ref.children.discard)


async def stop_children():
    """Graph fan-out cannot outlive its owning attempt, even after gather fails."""
    ref = current_attempt()
    if ref is None:
        return
    ref.closing = True
    tasks = [task for task in ref.children if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        from execution.persistence import resources
        from execution.infra.types import ProcessExitRequired
        runtime = resources()
        limit = OperationDeadline.after(runtime.config.cleanup_grace)
        if runtime._closing_deadline:
            limit = OperationDeadline(min(limit.when, runtime._closing_deadline.when))
        async def drain():
            _, pending = await asyncio.wait(tasks, timeout=limit.remaining())
            if pending:
                runtime.request_exit('graph_cleanup_grace_expired')
                raise ProcessExitRequired('Graph child tasks did not terminate')
        await runtime.cleanup(drain())
