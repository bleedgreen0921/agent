from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID_REQUEST = "INVALID_REQUEST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VERSION_IN_PROGRESS = "VERSION_IN_PROGRESS"
    RAG_UNAVAILABLE = "RAG_UNAVAILABLE"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    RUN_TIMEOUT = "RUN_TIMEOUT"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    INVALID_PLAN = "INVALID_PLAN"
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
    request_id: str


class RunCreate(StrictModel):
    task: str = Field(min_length=1)
    mode: Literal["react", "plan_execute"] = "react"


class RunAccepted(StrictModel):
    run_id: str
    status: str
    created_at: datetime


class SourceLocator(StrictModel):
    kind: Literal["pdf", "docx", "markdown", "txt"]
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] | None = None
    block_start: int | None = None
    block_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class Evidence(StrictModel):
    evidence_id: str
    document_id: str
    document_version_id: str
    title: str
    content: str
    source_locator: SourceLocator
    rank: int | None = None


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class EvidenceSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    request_id: str
    retrieval_id: str
    status: Literal["ok", "no_hits", "degraded"]
    evidences: list[Evidence]
    degradations: list[str]


class Claim(StrictModel):
    text: str
    support: Literal["evidence", "unverified"]
    evidence_ids: list[str]
    reason: str | None = None


class Result(StrictModel):
    answer: str
    claims: list[Claim]
    citations: list[Evidence]
    notices: list[dict[str, str]]


class RunResponse(StrictModel):
    run_id: str
    mode: Literal["react", "plan_execute"]
    status: Literal["queued", "running", "cancelling", "completed", "partial", "failed", "cancelled"]
    created_at: datetime
    finished_at: datetime | None = None
    result: Result | None = None
    error: ErrorDetail | None = None
