from fastapi import FastAPI

from contracts.errors import install_errors
from agent_service.api import admin_router, router

app = FastAPI(title="Agent service", version="1.0.0")
install_errors(app)
app.include_router(router)
app.include_router(admin_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent"}
