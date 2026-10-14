from datetime import datetime
from uuid import UUID
from fastapi import APIRouter, Request, Header, Query, Response
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, RunAccepted, RunCreate, RunList, RunQueueSummary, RunResponse, RunStatus, RunTimeline, WorkerSnapshot
from db.async_queries import worker_snapshot
from execution.infra.types import ResourceError
import psycopg
from agent_service.operations import list_runs, run_queue_summary
from agent_service.runs import cancel_run, create_run, get_run, timeline, trace
router = APIRouter(prefix='/v1/runs', tags=['runs'])
worker_router = APIRouter(prefix='/v1/workers', tags=['workers'])

@worker_router.get('', response_model=WorkerSnapshot)
async def worker_status(request: Request):
    try:
        return await worker_snapshot(request.app.state.resources, 'agent')
    except (ResourceError, psycopg.Error, OSError, TimeoutError):
        raise ApiError(503, ErrorCode.INTERNAL_ERROR, 'Worker status unavailable') from None

@router.get('', response_model=RunList)
async def read_runs(status: RunStatus | None=None, created_after: datetime | None=None, created_before: datetime | None=None, limit: int=Query(30, ge=1, le=100), cursor: str | None=None):
    return await list_runs(status=status, created_after=created_after, created_before=created_before, limit=limit, cursor=cursor)

@router.get('/summary', response_model=RunQueueSummary)
async def queue_summary():
    return await run_queue_summary()

@router.post('', response_model=RunAccepted)
async def submit_run(body: RunCreate, response: Response, idempotency_key: str | None=Header(default=None, alias='Idempotency-Key')):
    payload, replay = await create_run(body, idempotency_key)
    response.status_code = 200 if replay else 202
    return payload

@router.get('/{run_id}', response_model=RunResponse, response_model_exclude_none=True)
async def read_run(run_id: UUID):
    return await get_run(run_id)

@router.post('/{run_id}/cancel')
async def cancel(run_id: UUID, response: Response):
    payload, status = await cancel_run(run_id)
    response.status_code = status
    return payload

@router.get('/{run_id}/trace')
async def read_trace(run_id: UUID):
    return await trace(run_id)

@router.get('/{run_id}/timeline', response_model=RunTimeline)
async def read_timeline(run_id: UUID):
    return await timeline(run_id)
