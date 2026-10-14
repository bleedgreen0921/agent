"""Pure rules; deliberately independent of HTTP, databases and model frameworks."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

CONTRACT_VERSION = 1

class TaskKind(StrEnum):
    AGENT = 'agent_run'
    MEMORY = 'memory_job'
    DOCUMENT = 'rag_document'

class TaskStatus(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    STOPPING = 'stopping'
    COMPLETED = 'completed'
    PARTIAL = 'partial'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

TERMINAL = frozenset({'completed', 'partial', 'failed', 'cancelled'})

class StopReason(StrEnum):
    CANCEL = 'user_cancel'
    DEADLINE = 'deadline'
    TAKEOVER = 'takeover'
    LEASE = 'lease_expired'
    EXIT = 'worker_exit'
    DATABASE = 'database_unavailable'

@dataclass(frozen=True)
class TaskRef:
    id: UUID
    kind: TaskKind

@dataclass(frozen=True)
class LeaseSnapshot:
    task: TaskRef
    token: UUID | None
    status: str
    leased_until: datetime | None
    deadline_at: datetime | None
    stop_requested_at: datetime | None = None

    def refusal(self, task: TaskRef, token: UUID, now: datetime) -> str | None:
        if self.status in TERMINAL:
            return 'terminal'
        if self.task != task or self.token != token:
            return StopReason.TAKEOVER
        if self.stop_requested_at is not None or self.status == 'stopping':
            return StopReason.CANCEL
        if self.deadline_at is None or now >= self.deadline_at:
            return StopReason.DEADLINE
        if self.leased_until is None or now >= self.leased_until:
            return StopReason.LEASE
        if self.status != 'running':
            return 'inactive'
        return None

@dataclass(frozen=True)
class ExecutionContext:
    task: TaskRef
    lease_token: UUID
    deadline_at: datetime
    step_id: UUID | None = None
    call_id: UUID | None = None
    rag_timeout_seconds: float | None = None
    inputs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        object.__setattr__(self, 'inputs', freeze(json.loads(json.dumps(thaw(self.inputs), allow_nan=False))))

    @property
    def run_id(self) -> UUID:
        return self.task.id

    def business_call_id(self, _external_id: str | None = None) -> UUID:
        if self.call_id is None:
            raise RuntimeError('Tool call is missing its persistent trace ID')
        return self.call_id

    def timeout(self, configured: float, now: datetime) -> float:
        return bounded_timeout(configured, self.deadline_at, now)


def bounded_timeout(configured: float, deadline: datetime, now: datetime) -> float:
    if configured <= 0:
        raise ValueError('Configured timeout must be positive')
    remaining = (deadline - now).total_seconds()
    if remaining <= 0:
        raise PermissionError('Execution deadline has expired')
    return min(configured, remaining)


def public_status(kind: TaskKind | str, status: str, attempts: int = 0) -> str:
    if kind == TaskKind.AGENT:
        return 'cancelling' if status == 'stopping' else status
    if kind == TaskKind.MEMORY:
        return {'queued': 'pending', 'completed': 'done'}.get(status, status)
    return 'retry' if status == 'queued' and attempts else 'done' if status == 'completed' else status


def freeze(value: Any) -> Any:
    """Copy JSON values into a recursively immutable tree."""
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value

@dataclass(frozen=True)
class ToolOutcome:
    observations: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    notices: tuple[Mapping[str, str], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        payload = json.loads(json.dumps({'evidence': thaw(self.evidence), 'notices': thaw(self.notices),
                                        'metadata': thaw(self.metadata)}, allow_nan=False))
        if not isinstance(self.observations, str):
            raise ValueError('Tool observations must be text')
        allowed = {'status','result_summary','service_request_id','retrieval_id','evidence_ids','error_code'}
        if set(payload['metadata']) - allowed:
            raise ValueError('Unsafe tool metadata')
        if payload['metadata'].get('status', 'succeeded') not in {'succeeded','failed','degraded'}:
            raise ValueError('Invalid tool settlement status')
        for key in ('service_request_id','retrieval_id','error_code'):
            if key in payload['metadata'] and payload['metadata'][key] is not None and not isinstance(payload['metadata'][key],str):
                raise ValueError('Invalid tool metadata type')
        if 'evidence_ids' in payload['metadata'] and (not isinstance(payload['metadata']['evidence_ids'],list) or not all(isinstance(v,str) for v in payload['metadata']['evidence_ids'])):
            raise ValueError('Invalid evidence IDs')
        if 'result_summary' in payload['metadata'] and not isinstance(payload['metadata']['result_summary'],dict):
            raise ValueError('Invalid result summary')
        for notice in payload['notices']:
            if set(notice) != {'code','message'} or not all(isinstance(v,str) and v for v in notice.values()):
                raise ValueError('Invalid tool notice')
        object.__setattr__(self, 'evidence', freeze(payload['evidence']))
        object.__setattr__(self, 'notices', freeze(payload['notices']))
        object.__setattr__(self, 'metadata', freeze(payload['metadata']))

    def to_artifact(self) -> dict:
        return {'contract_version': CONTRACT_VERSION, 'observations': self.observations,
                'evidence': thaw(self.evidence), 'notices': thaw(self.notices), 'metadata': thaw(self.metadata)}

    @classmethod
    def from_artifact(cls, data: dict) -> 'ToolOutcome':
        if not isinstance(data, dict) or data.get('contract_version') != CONTRACT_VERSION or set(data) != {
            'contract_version','observations','evidence','notices','metadata'}:
            raise ValueError('Unsupported tool outcome contract')
        return cls(data['observations'], tuple(data['evidence']), tuple(data['notices']), data['metadata'])

@dataclass(frozen=True)
class FailureResult:
    accepted: bool
    status: str | None = None
    error_code: str | None = None

@dataclass
class WorkHandle:
    """Logical stop never releases capacity while physical work is still alive."""
    task: TaskRef
    stopped: bool = False
    physically_done: bool = False

    def request_stop(self) -> None:
        self.stopped = True

    @property
    def occupies_capacity(self) -> bool:
        return not self.physically_done
