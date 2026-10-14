"""Control signals are never provider failures or optional-feature degradation."""
import asyncio
import psycopg
from execution.persistence import PersistenceOutcomeUnknown
from execution.infra.types import ResourceClosed, WorkStopped, ProcessExitRequired


class ExecutionRevoked(PermissionError):
    """The persistent or local executor no longer has permission to advance."""


CONTROL_ERRORS = (asyncio.CancelledError, ExecutionRevoked, PersistenceOutcomeUnknown,
                  psycopg.Error, ResourceClosed, WorkStopped, ProcessExitRequired)


def select_failure(error):
    """Flatten TaskGroup failures; control takes precedence over business failure."""
    if not isinstance(error, BaseExceptionGroup):
        # SDKs may wrap a control refusal from the managed transport. Preserve
        # the original signal rather than reporting it as a connection failure.
        cause = error
        visited = set()
        while cause is not None and id(cause) not in visited:
            visited.add(id(cause))
            if isinstance(cause, CONTROL_ERRORS):
                return cause
            cause = cause.__cause__
        return error
    errors = [select_failure(item) for item in error.exceptions]
    return next((item for item in errors if isinstance(item, CONTROL_ERRORS)),
                next((item for item in errors if getattr(item, 'retryable', True) is False), errors[0]))
