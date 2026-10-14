"""Business dispatch, failure classification and durable attempt finalization."""
import asyncio
import httpx
import openai
import psycopg
from execution.contracts import TaskKind
from execution.errors import CONTROL_ERRORS, select_failure
from execution.infra.types import CpuPoolReset
from execution.outcomes import AttemptOutcome, OutcomeKind
from execution.persistence import bind, PersistenceOutcomeUnknown
from execution.scope import attempt


async def finish(context, job, *, failure=None, retryable=False):
    kind = context.task.kind
    if kind == TaskKind.AGENT:
        from agent_service.runtime import attempt_exited, fail_run
        if failure:
            return await fail_run(context.task.id, context.token, failure)
        return await attempt_exited(context.task.id, context.token)
    if kind == TaskKind.MEMORY:
        from agent_service.storage.memory import finish_job
        return await finish_job(job, failure or 'WORKER_INTERRUPTED',
                                retryable=retryable or failure is None, worker_exit=True)
    from rag_service.storage.jobs import fail
    return await fail(job, failure or 'WORKER_INTERRUPTED', retryable or failure is None, worker_exit=True)


def classify(exc):
    from execution.flow import DownstreamLimited
    cause, visited = exc, set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, DownstreamLimited): return 'MODEL_RATE_LIMITED', True
        cause = cause.__cause__
    if isinstance(exc, (openai.RateLimitError, DownstreamLimited)):
        return "MODEL_RATE_LIMITED", True
    from rag_service.parse import ParseFailure
    from execution.providers import ProviderFailure
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return 'MODEL_AUTH_FAILED', False
    if isinstance(exc, ProviderFailure):
        return exc.code, exc.retryable
    if isinstance(exc, ParseFailure):
        return str(exc), False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return 'MODEL_TIMEOUT', True
    if isinstance(exc, CpuPoolReset):
        return 'CPU_POOL_RESET', True
    return 'PROCESSING_FAILED', False


async def execute(context, job, runtime):
    try:
        with bind(runtime, deadline=context.deadline), attempt(context.task, context.token,
                context.stopped, deadline=context.deadline):
            async with asyncio.timeout_at(context.deadline.when):
                if context.task.kind == TaskKind.AGENT:
                    from agent_service.execution import execute_run
                    return await execute_run(context.task.id, context.token)
                if context.task.kind == TaskKind.MEMORY:
                    from agent_service.memory import process_job
                    await process_job(job)
                else:
                    from rag_service.processing import process
                    await process(job)
                return AttemptOutcome(OutcomeKind.FINISHED)
    except asyncio.CancelledError:
        raise
    except BaseException as original:
        exc = select_failure(original)
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        if isinstance(exc, CONTROL_ERRORS):
            kind = OutcomeKind.UNKNOWN if isinstance(exc, PersistenceOutcomeUnknown) else OutcomeKind.RECONCILE
            return AttemptOutcome(kind, detail=type(exc).__name__)
        if not isinstance(exc, Exception):
            raise exc
        code, retry = classify(exc)
        try:
            with bind(runtime):
                if await finish(context, job, failure=code, retryable=retry):
                    return AttemptOutcome(OutcomeKind.FINISHED, code)
        except CONTROL_ERRORS:
            pass
        return AttemptOutcome(OutcomeKind.RECONCILE, code)
