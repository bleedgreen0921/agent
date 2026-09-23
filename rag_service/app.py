from fastapi import FastAPI

from contracts.errors import install_errors
from identity.api import router as identity_router
from rag_service.api import admin_router, evidence_router

app = FastAPI(title="RAG service", version="1.0.0")
install_errors(app)
app.include_router(identity_router)
app.include_router(admin_router)
app.include_router(evidence_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "rag"}
