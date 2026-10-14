"""Call coordination; providers never reserve, settle or create recovery receipts."""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4
from execution.contracts import TaskRef
from execution.infra.types import OperationDeadline, WorkOwner
from execution.errors import CONTROL_ERRORS, ExecutionRevoked, select_failure
from execution.persistence import call_scope, AlreadyApplied, Rejected, PersistenceOutcomeUnknown
from execution.scope import authorize, local_authority, track_call_task, call_identity


@dataclass(frozen=True)
class CallContext:
    task: TaskRef
    token: UUID
    deadline: OperationDeadline
    step_id: UUID | None = None
    purpose: str = ''
    id: UUID = field(default_factory=uuid4)

    @property
    def owner(self):
        return WorkOwner(self.task, self.token, self.id)


@dataclass(frozen=True)
class ProviderReply:
    value: Any
    metadata: dict = field(default_factory=dict)
    status: str = 'succeeded'


class CallRunner:
    async def run(self, context: CallContext, *, reserve: Callable[[UUID], Awaitable],
                  invoke: Callable[[CallContext], Awaitable[ProviderReply]],
                  settle: Callable[..., Awaitable], failure: Callable):
        # IDs are allocated before the transaction. No runner retries a call.
        track_call_task()
        reserved = False
        try:
            with call_scope(context.deadline), call_identity(context.id):
                async with asyncio.timeout_at(context.deadline.when):
                    local_authority(context.task, context.token)
                    reservation = await reserve(context.id)
                    if isinstance(reservation, Rejected):
                        raise ExecutionRevoked(reservation.reason)
                    if isinstance(reservation, AlreadyApplied):
                        raise PersistenceOutcomeUnknown('Call already reserved; external dispatch cannot be inferred')
                    reserved = True
                    await authorize()
                    from execution.progress import activity
                    async with activity('call:' + context.purpose, context.deadline, identity=context.id):
                        reply = await invoke(context)
                    if not isinstance(reply, ProviderReply):
                        raise TypeError('Adapters must return ProviderReply')
        except BaseException as exc:
            exc = select_failure(exc)
            if isinstance(exc, CONTROL_ERRORS) or not reserved:
                raise exc
            code, metadata, mapped = failure(exc)
            # Use the attempt deadline and DB profile after a request timeout.
            # A settlement failure must never trigger another settlement.
            if not await settle(context.id, 'failed', error_code=code, **metadata):
                raise ExecutionRevoked('Call settlement rejected') from exc
            if mapped is exc:
                raise exc
            raise mapped from exc
        if not await settle(context.id, reply.status, **reply.metadata):
            raise ExecutionRevoked('Call settlement rejected')
        local_authority(context.task, context.token)
        return reply
