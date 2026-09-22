from fastapi import FastAPI

from contracts.errors import install_errors
from identity.api import router as identity_router

app = FastAPI(title="RAG service", version="1.0.0")
install_errors(app)
app.include_router(identity_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "rag"}
