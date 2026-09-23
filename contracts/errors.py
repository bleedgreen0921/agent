import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from contracts.v1 import ErrorCode


log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str):
        self.status = status
        self.code = code
        self.message = message


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}, "request_id": request.state.request_id})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": ErrorCode.INVALID_REQUEST, "message": "Invalid request"}, "request_id": request.state.request_id})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.FORBIDDEN if exc.status_code == 403 else ErrorCode.UNAUTHENTICATED if exc.status_code == 401 else ErrorCode.INVALID_REQUEST
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": code, "message": "Request failed"}, "request_id": request.state.request_id})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        log.exception("Unhandled API error", exc_info=exc)
        return JSONResponse(status_code=500, content={"error": {"code": ErrorCode.INTERNAL_ERROR, "message": "Internal server error"}, "request_id": request.state.request_id})

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response
