from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response

from contracts.v1 import RunAccepted, RunCreate, RunResponse, RunTimeline
from identity.security import Principal, agent_admin, agent_team
from agent_service.runs import cancel_run, create_run, get_run, timeline, trace


router = APIRouter(prefix="/v1/runs", tags=["runs"])
admin_router = APIRouter(prefix="/v1/admin/runs", tags=["run trace"])


@router.post("", response_model=RunAccepted)
def submit_run(body: RunCreate, response: Response, principal: Principal = Depends(agent_team), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    payload, replay = create_run(body, principal, idempotency_key)
    response.status_code = 200 if replay else 202
    return payload


@router.get("/{run_id}", response_model=RunResponse, response_model_exclude_none=True)
def read_run(run_id: UUID, principal: Principal = Depends(agent_team)):
    return get_run(run_id, principal.team_id)


@router.post("/{run_id}/cancel")
def cancel(run_id: UUID, response: Response, principal: Principal = Depends(agent_team)):
    payload, status = cancel_run(run_id, principal.team_id)
    response.status_code = status
    return payload


@admin_router.get("/{run_id}/trace", dependencies=[Depends(agent_admin)])
def read_trace(run_id: UUID):
    return trace(run_id)


@admin_router.get("/{run_id}/timeline", response_model=RunTimeline, dependencies=[Depends(agent_admin)])
def read_timeline(run_id: UUID):
    return timeline(run_id)
