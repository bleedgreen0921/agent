"""Finite commit-outcome verification. It never dispatches or replays providers."""
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID
import psycopg
from execution.contracts import TaskRef
from execution.infra.types import ResourceError


@dataclass(frozen=True)
class OperationIdentity:
    kind: str
    task: TaskRef
    id: UUID
    token: UUID = field(repr=False)


class ResolutionState(StrEnum):
    APPLIED = 'applied'
    RECORDED = 'recorded_no_dispatch'
    NOT_APPLIED = 'not_applied'
    REJECTED = 'rejected'
    UNRESOLVED = 'unresolved'


@dataclass(frozen=True)
class Resolution:
    state: ResolutionState
    value: UUID | None = None


async def verify_unknown(error, operation, inspect):
    """One bounded query operation; only known rollbacks within it may retry.

    An absent record can only be reported after the inspector obtains the task
    lock, which waits for any still-committing writer before taking a new view.
    """
    from execution.persistence import PersistenceOutcomeUnknown
    error.operation = operation
    try:
        error.resolution = await inspect()
    except (PersistenceOutcomeUnknown, psycopg.Error, ResourceError, TimeoutError):
        error.resolution = Resolution(ResolutionState.UNRESOLVED)
    return error.resolution
