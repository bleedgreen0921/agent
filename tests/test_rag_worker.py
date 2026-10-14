"""Async supervision replaces document-child process polling."""
import asyncio
from uuid import uuid4
import psycopg
import pytest
from execution.worker import AttemptHandle,execute,stop,settle
from execution.contracts import TaskRef,TaskKind
from execution.infra.types import WorkOwner,OperationDeadline
from execution.persistence import resources


def handle(kind):
    identity,token=uuid4(),uuid4()
    return AttemptHandle(WorkOwner(TaskRef(identity,kind),token),{'id':identity,'token':token,'lease_token':token},OperationDeadline.after(2))


async def test_processing_database_failure_leaves_persistent_lease_for_controller(monkeypatch):
    from rag_service import processing
    async def unavailable(job):raise psycopg.OperationalError('database unavailable')
    monkeypatch.setattr(processing,'process',unavailable)
    attempt=handle(TaskKind.DOCUMENT)
    await execute(attempt,resources())
    assert attempt.owner.attempt in resources().stopped_attempts
    assert not attempt.settled


async def test_cancellation_stops_only_its_attempt_and_tracks_actual_work(monkeypatch):
    from rag_service import processing
    entered=asyncio.Event()
    async def wait(job):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(processing,'process',wait)
    first,second=handle(TaskKind.DOCUMENT),handle(TaskKind.DOCUMENT)
    first.task=asyncio.create_task(execute(first,resources()))
    await entered.wait();entered.clear()
    second.task=asyncio.create_task(execute(second,resources()));await entered.wait()
    await stop(first,resources())
    with pytest.raises(asyncio.CancelledError):await first.task
    assert not second.task.done()
    await stop(second,resources())
    with pytest.raises(asyncio.CancelledError):await second.task


async def test_known_document_failure_uses_explicit_retry_policy(monkeypatch):
    from rag_service import processing
    from rag_service.storage import jobs
    from rag_service.models import ModelFailure
    recorded=[]
    async def failure(job):raise ModelFailure('EMBEDDING_UNAVAILABLE',True)
    async def record(job,code,retryable,**kwargs):recorded.append((code,retryable));return True
    monkeypatch.setattr(processing,'process',failure);monkeypatch.setattr(jobs,'fail',record)
    await execute(handle(TaskKind.DOCUMENT),resources())
    assert recorded==[('EMBEDDING_UNAVAILABLE',True)]
