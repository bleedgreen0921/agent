from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from contracts.errors import install_errors
from db.config_check import validate_role
from db.readiness import rag_ready
from identity.api import router as identity_router, team_router
from rag_service.api import admin_router, evidence_router, worker_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_role("rag-api")
    yield


app = FastAPI(title="RAG service", version="1.0.0", lifespan=lifespan)
install_errors(app)
app.include_router(identity_router)
app.include_router(team_router)
app.include_router(admin_router)
app.include_router(worker_router)
app.include_router(evidence_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "rag"}


@app.get("/health/ready")
def readiness(response: Response):
    ready = rag_ready()
    response.status_code = 200 if ready else 503
    return {"service": "rag", "status": "ok" if ready else "unavailable"}
