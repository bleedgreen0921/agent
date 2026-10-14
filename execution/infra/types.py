"""Local resource contracts; no persistent execution or budget semantics."""
import asyncio
import math
import os
from dataclasses import dataclass
from uuid import UUID

from execution.contracts import TaskRef


class ResourceError(RuntimeError):
    pass


class ResourceClosed(ResourceError):
    pass


class CapacityTimeout(ResourceError, TimeoutError):
    pass


class ResourceUnavailable(ResourceError):
    pass


class CpuPoolReset(ResourceUnavailable):
    pass


class ProcessExitRequired(ResourceUnavailable):
    pass


class WorkStopped(ResourceError):
    pass


@dataclass(frozen=True)
class WorkOwner:
    task: TaskRef
    lease_token: UUID
    call_id: UUID | None = None

    @property
    def attempt(self):
        return self.task, self.lease_token


@dataclass(frozen=True)
class OperationDeadline:
    when: float

    def __post_init__(self):
        if not math.isfinite(self.when):
            raise ValueError('Operation deadline must be finite')

    @classmethod
    def after(cls, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Operation timeout must be finite and positive')
        return cls(asyncio.get_running_loop().time() + seconds)

    def remaining(self):
        return max(0., self.when - asyncio.get_running_loop().time())


@dataclass
class WorkRecord:
    backend: str
    owner: WorkOwner | None
    generation: int = 0
    state: str = 'queued'
    stop_reason: str | None = None
    work_id: UUID | None = None

    @property
    def occupies_capacity(self):
        return self.state == 'submitted'


@dataclass(frozen=True)
class ResourceConfig:
    db_control: int = 2
    db_normal: int = 4
    db_bulk: int = 1
    db_waiting: int = 32
    db_acquire: float = 5
    http_connections: int = 16
    http_keepalive: int = 8
    http_concurrency: int = 8
    http_pending: int = 16
    io_workers: int = 4
    io_pending: int = 16
    cpu_workers: int = 1
    cpu_pending: int = 4
    cleanup_grace: float = 5
    cpu_reset_timeout: float = 30
    exit_grace: float = 30

    def __post_init__(self):
        for name, field in self.__dataclass_fields__.items():
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0 or (field.type is int and (not isinstance(value, int) or isinstance(value, bool))):
                raise ValueError(f'{name} must be positive and finite')
        if self.http_keepalive > self.http_connections:
            raise ValueError('HTTP keepalive capacity exceeds connection capacity')

    @classmethod
    def from_env(cls):
        values = {}
        names = {'cleanup_grace': 'EXECUTOR_CLEANUP_GRACE_SECONDS',
                 'exit_grace': 'WORKER_EXIT_GRACE_SECONDS',
                 'cpu_reset_timeout': 'CPU_POOL_RESET_TIMEOUT_SECONDS',
                 'db_acquire': 'ASYNC_DB_ACQUIRE_TIMEOUT_SECONDS'}
        for name, field in cls.__dataclass_fields__.items():
            env = names.get(name, 'ASYNC_' + name.upper())
            values[name] = field.type(os.environ.get(env, str(field.default)))
        return cls(**values)
