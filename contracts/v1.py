from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorCode(StrEnum):
    INPUT_TOKEN_LIMIT_EXCEEDED = 'INPUT_TOKEN_LIMIT_EXCEEDED'
    SUBMISSION_RATE_LIMITED = "SUBMISSION_RATE_LIMITED"
    QUEUE_CAPACITY_EXCEEDED = "QUEUE_CAPACITY_EXCEEDED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    RAG_RATE_LIMITED = "RAG_RATE_LIMITED"
    TOOL_BATCH_TOO_LARGE = "TOOL_BATCH_TOO_LARGE"
    TOOL_CAPACITY_EXCEEDED = "TOOL_CAPACITY_EXCEEDED"
    EXECUTION_STALLED = "EXECUTION_STALLED"
    CPU_POOL_RESET = "CPU_POOL_RESET"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID_REQUEST = "INVALID_REQUEST"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VERSION_IN_PROGRESS = "VERSION_IN_PROGRESS"
    RAG_UNAVAILABLE = "RAG_UNAVAILABLE"
    RAG_TIMEOUT = "RAG_TIMEOUT"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    RUN_TIMEOUT = "RUN_TIMEOUT"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    INVALID_PLAN = "INVALID_PLAN"
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INTERRUPTED_UNKNOWN = "INTERRUPTED_UNKNOWN"
    CONVERSATION_BUSY = "CONVERSATION_BUSY"


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
    request_id: str


class ToolAuthorization(StrictModel):
    tool_name: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    exact_arguments: dict[str, Any]
    max_calls: int = Field(default=1, ge=1, le=10)

class RunCreate(StrictModel):
    task: str = Field(min_length=1)
    mode: Literal["react", "plan_execute"] = "react"
    tool_authorizations: list[ToolAuthorization] = Field(default_factory=list, max_length=10)


RunStatus = Literal["queued", "running", "cancelling", "completed", "partial", "failed", "cancelled"]


class RunAccepted(StrictModel):
    run_id: str
    status: RunStatus
    created_at: datetime


class RunListItem(StrictModel):
    id: UUID
    mode: Literal["react", "plan_execute"]
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None


class RunList(StrictModel):
    items: list[RunListItem]
    next_cursor: str | None


class RunQueueSummary(StrictModel):
    queued_within_deadline: int
    queued_past_deadline: int
    running_lease_valid: int
    running_lease_expired: int
    cancelling: int
    oldest_claimable_created_at: datetime | None


class PhaseProgress(StrictModel):
    waiting_reason: str | None = None
    stage: str
    started_at: datetime
    deadline_at: datetime


class ExecutionProgress(StrictModel):
    last_progress_at: datetime | None
    sequence: int
    observed_at: datetime
    activities: list[PhaseProgress]
    draining: bool | None = None
    draining_observed_at: datetime | None = None
    waiting_reason: str | None = None


class WorkerInstanceStatus(StrictModel):
    instance_id: UUID
    started_at: datetime
    heartbeat_at: datetime
    online: bool
    memory_consumer_alive: bool | None = None
    resources: dict[str, Any] | None = None


class WorkerSnapshot(StrictModel):
    service: Literal["agent", "rag"]
    observed_at: datetime
    online_count: int
    last_heartbeat_at: datetime | None
    queued_count: int
    oldest_queued_at: datetime | None
    overdue_count: int
    expired_lease_count: int
    instances: list[WorkerInstanceStatus]
    work_classes: list[dict[str, Any]] = Field(default_factory=list)
    downstream: list[dict[str, Any]] = Field(default_factory=list)
    alerts: list[dict[str, Any]] = Field(default_factory=list)
    admission: dict[str, Any] | None = None


class SourceLocator(StrictModel):
    kind: Literal["pdf", "docx", "markdown", "txt"]
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] | None = None
    block_start: int | None = None
    block_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class ExperimentSourceFile(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentSourceLocator(StrictModel):
    kind: Literal["experiment"] = "experiment"
    project_id: str
    experiment_ids: list[str]
    source_files: list[ExperimentSourceFile]
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    method: str
    method_version: str
    synthetic: bool


class Evidence(StrictModel):
    evidence_id: str
    document_id: str | None = None
    document_version_id: str | None = None
    title: str
    content: str
    source_locator: SourceLocator | ExperimentSourceLocator = Field(discriminator="kind")
    rank: int | None = None

    @model_validator(mode="after")
    def validate_source_identity(self):
        if isinstance(self.source_locator, SourceLocator):
            if not self.document_id or not self.document_version_id:
                raise ValueError("Document evidence requires document and version IDs")
        elif self.document_id is not None or self.document_version_id is not None:
            raise ValueError("Experiment evidence must use experiment source identity")
        return self


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class TokenUsage(StrictModel):
    token_limit: int | None = None
    charged: int = Field(default=0,ge=0)
    blocked: bool = False
    calls: int = Field(default=0,ge=0)
    actual_input_tokens: int = Field(default=0,ge=0)
    actual_output_tokens: int = Field(default=0,ge=0)
    unconfirmed_tokens: int = Field(default=0,ge=0)
    estimated_calls: int = Field(default=0,ge=0)


class EvidenceSearchResponse(BaseModel):
    usage: TokenUsage | None = None
    model_config = ConfigDict(extra="ignore")
    request_id: str
    retrieval_id: str
    status: Literal["ok", "no_hits", "degraded"]
    evidences: list[Evidence]
    degradations: list[str]


class DocumentVersionRef(StrictModel):
    document_id: str
    document_version_id: str
    status: Literal["pending", "processing", "active", "superseded", "failed"]


class DocumentVersionStatus(DocumentVersionRef):
    progress: ExecutionProgress | None = None
    attempts: int = Field(ge=0)
    error_code: str | None
    warnings: list[str]


class DocumentDeleted(StrictModel):
    document_id: str
    deleted_at: datetime


class Claim(StrictModel):
    text: str
    support: Literal["evidence", "unverified"]
    evidence_ids: list[str]
    reason: str | None = None


class RequirementAcceptance(StrictModel):
    requirement_id: str
    description: str
    status: Literal['met','not_met','uncertain']
    reason: str

class ClaimAcceptance(StrictModel):
    claim_id: str
    text: str
    status: Literal['supported','unsupported','contradicted','insufficient']
    reason: str

class AcceptanceReport(StrictModel):
    policy_version: int
    status: Literal['accepted','partial']
    requirements: list[RequirementAcceptance]
    claims: list[ClaimAcceptance]
    issues: list[str]

class Result(StrictModel):
    answer: str
    claims: list[Claim]
    citations: list[Evidence]
    notices: list[dict[str, str]]
    acceptance: AcceptanceReport | None = None


class RunResponse(StrictModel):
    token_usage: TokenUsage | None = None
    progress: ExecutionProgress | None = None
    run_id: str
    mode: Literal["react", "plan_execute"]
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None = None
    result: Result | None = None
    error: ErrorDetail | None = None


TimelineEventType = Literal[
    "run_created",
    "manifest_captured",
    "run_started",
    "step",
    "model_call",
    "tool_call",
    "result_published",
    "run_finished",
    "verification",
]


class TimelineManifest(StrictModel):
    schema_version: int
    captured_at: datetime
    data: dict[str, Any]


class TimelineEvent(StrictModel):
    sequence: int
    event_id: str
    type: TimelineEventType
    occurred_at: datetime
    finished_at: datetime | None = None
    status: str | None = None
    entity_id: str | None = None
    step_id: str | None = None
    parent_id: str | None = None
    name: str | None = None
    details: dict[str, Any]


class RunTimeline(StrictModel):
    run_id: str
    status: RunStatus
    manifest: TimelineManifest | None
    events: list[TimelineEvent]
