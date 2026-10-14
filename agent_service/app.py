from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, Request

from contracts.errors import install_errors
from contracts.local import LoopbackOnly
from db.config_check import validate_role
from db.async_queries import ready
from execution.infra.runtime import RuntimeResources
from execution.persistence import ResourceBinding
from agent_service.api import router, worker_router
from agent_service.conversations import router as conversation_router, memory_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_role("agent-api")
    async with RuntimeResources(role="agent-api", on_exit=getattr(_app.state, "resource_exit_handler", None)) as resources:
        await resources.http.register_available()
        _app.state.resources = resources
        yield


app = FastAPI(title="Agent service", version="1.0.0", lifespan=lifespan)
install_errors(app)
app.add_middleware(ResourceBinding)
app.add_middleware(LoopbackOnly)
app.include_router(router)
app.include_router(worker_router)
app.include_router(conversation_router)
app.include_router(memory_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent"}


@app.get("/health/ready")
async def readiness(response: Response, request: Request):
    available = await ready(request.app.state.resources, "agent")
    response.status_code = 200 if available else 503
    return {"service": "agent", "status": "ok" if available else "unavailable"}


@app.get('/health/accepting')
async def accepting(response: Response):
    from execution.admission import AdmissionStore
    from execution.infra.types import ResourceError
    import psycopg
    try:
        states = await AdmissionStore().capabilities('agent')
    except (ResourceError, psycopg.Error, TimeoutError):
        response.status_code = 503
        return {'service': 'agent', 'accepting': False, 'reason': 'CONTROL_UNAVAILABLE'}
    response.status_code = 200 if any(s['accepting'] for s in states.values()) else 503
    return {'capabilities': states, 'accepting': response.status_code == 200, 'service': 'agent'}
