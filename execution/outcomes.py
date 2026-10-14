"""Transient pipeline outcomes consumed by the task supervisor."""
from dataclasses import dataclass
from enum import StrEnum


class OutcomeKind(StrEnum):
    FINISHED = 'finished'
    RECONCILE = 'reconcile'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class AttemptOutcome:
    kind: OutcomeKind
    error_code: str | None = None
    detail: str | None = None
