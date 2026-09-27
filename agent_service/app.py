from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from contracts.errors import install_errors
from db.config_check import validate_role
from db.readiness import agent_ready
from agent_service.api import admin_router, router, worker_router
from agent_service.conversations import router as conversation_router, memory_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_role("agent-api")
    yield


app = FastAPI(title="Agent service", version="1.0.0", lifespan=lifespan)
install_errors(app)
app.include_router(router)
app.include_router(admin_router)
app.include_router(worker_router)
app.include_router(conversation_router)
app.include_router(memory_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent"}


@app.get("/health/ready")
def readiness(response: Response):
    ready = agent_ready()
    response.status_code = 200 if ready else 503
    return {"service": "agent", "status": "ok" if ready else "unavailable"}
