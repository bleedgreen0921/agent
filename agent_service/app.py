from fastapi import FastAPI

from contracts.errors import install_errors

app = FastAPI(title="Agent service", version="1.0.0")
install_errors(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent"}
