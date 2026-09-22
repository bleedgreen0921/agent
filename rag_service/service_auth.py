import hmac
import os

from fastapi import Header

from contracts.errors import ApiError
from contracts.v1 import ErrorCode


def require_service(authorization: str | None = Header(default=None)) -> None:
    configured = os.environ.get("RAG_SERVICE_TOKEN", "")
    expected = "Bearer " + configured
    if len(configured) < 43 or not hmac.compare_digest(authorization or "", expected):
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Invalid credential")
