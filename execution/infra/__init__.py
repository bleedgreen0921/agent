"""Async infrastructure for API lifespans and future async Worker entry points."""
# Import RuntimeResources from execution.infra.runtime (avoids registry cycles).
from execution.infra.types import (WorkOwner, WorkRecord, OperationDeadline, ResourceConfig,
                                  ResourceError, ResourceClosed, CapacityTimeout,
                                  ResourceUnavailable, CpuPoolReset, ProcessExitRequired, WorkStopped)


def __getattr__(name):
    if name == 'RuntimeResources':
        from execution.infra.runtime import RuntimeResources
        return RuntimeResources
    raise AttributeError(name)
