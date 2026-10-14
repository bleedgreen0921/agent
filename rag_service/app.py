from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, Request

from contracts.errors import install_errors
from contracts.local import LoopbackOnly
from db.config_check import validate_role
from db.async_queries import ready
from execution.infra.runtime import RuntimeResources
from execution.persistence import ResourceBinding
from rag_service.api import document_router, evidence_router, worker_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_role("rag-api")
    async with RuntimeResources(role="rag-api", on_exit=getattr(_app.state, "resource_exit_handler", None)) as resources:
        await resources.http.register_available()
        _app.state.resources = resources
        yield


app = FastAPI(title="RAG service", version="1.0.0", lifespan=lifespan)
install_errors(app)
from execution.upload_admission import UploadAdmission
app.add_middleware(UploadAdmission)
app.add_middleware(ResourceBinding)
app.add_middleware(LoopbackOnly)
app.include_router(document_router)
app.include_router(worker_router)
app.include_router(evidence_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rag"}


@app.get("/health/ready")
async def readiness(response: Response, request: Request):
    available = await ready(request.app.state.resources, "rag")
    response.status_code = 200 if available else 503
    return {"service": "rag", "status": "ok" if available else "unavailable"}


@app.get('/health/accepting')
async def accepting(response: Response):
    from execution.admission import AdmissionStore
    from execution.infra.types import ResourceError
    import psycopg
    try:
        states = await AdmissionStore().capabilities('rag')
    except (ResourceError, psycopg.Error, TimeoutError):
        response.status_code = 503
        return {'service': 'rag', 'accepting': False, 'reason': 'CONTROL_UNAVAILABLE'}
    response.status_code = 200 if any(s['accepting'] for s in states.values()) else 503
    return {'capabilities': states, 'accepting': response.status_code == 200, 'service': 'rag'}
