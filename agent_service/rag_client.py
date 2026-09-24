"""HTTP-only adapter for Agent calls to the RAG evidence API."""

from dataclasses import dataclass
from uuid import UUID

import httpx

from contracts.v1 import ErrorCode, ErrorResponse, Evidence, EvidenceSearchRequest, EvidenceSearchResponse
from db.connection import connect


@dataclass(frozen=True)
class TrustedRunContext:
    run_id: UUID
    team_id: UUID

    @classmethod
    def from_persisted_run(cls, run_id: UUID) -> "TrustedRunContext":
        with connect("AGENT_DATABASE_URL") as conn:
            row = conn.execute("SELECT id, team_id FROM agent.agent_runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            raise LookupError("Run not found")
        return cls(row["id"], row["team_id"])


class RagError(Exception):
    def __init__(self, status: int, code: ErrorCode):
        self.status = status
        self.code = code
        super().__init__(code)


class RagClient:
    def __init__(self, base_url: str, service_token: str, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.client = client or httpx.Client(timeout=None)

    def search(self, run_id: UUID, tool_call_id: str, request: EvidenceSearchRequest) -> EvidenceSearchResponse:
        context = TrustedRunContext.from_persisted_run(run_id)
        try:
            response = self.client.post(
                self.base_url + "/v1/evidence/search",
                headers={"Authorization": "Bearer " + self.service_token, "X-Team-Id": str(context.team_id), "X-Run-Id": str(context.run_id), "X-Tool-Call-Id": tool_call_id},
                json=request.model_dump(),
            )
        except httpx.TransportError as exc:
            raise RagError(503, ErrorCode.RAG_UNAVAILABLE) from exc
        if response.is_error:
            raise self._error(response)
        try:
            return EvidenceSearchResponse.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise RagError(502, ErrorCode.RAG_UNAVAILABLE) from exc

    def read(self, run_id: UUID, tool_call_id: str, evidence_id: str) -> Evidence:
        context = TrustedRunContext.from_persisted_run(run_id)
        try:
            response = self.client.get(
                self.base_url + "/v1/evidence/" + evidence_id,
                headers={"Authorization": "Bearer " + self.service_token, "X-Team-Id": str(context.team_id), "X-Run-Id": str(context.run_id), "X-Tool-Call-Id": tool_call_id},
            )
        except httpx.TransportError as exc:
            raise RagError(503, ErrorCode.RAG_UNAVAILABLE) from exc
        if response.is_error:
            raise self._error(response)
        try:
            return Evidence.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise RagError(502, ErrorCode.RAG_UNAVAILABLE) from exc

    @staticmethod
    def _error(response: httpx.Response) -> RagError:
        try:
            detail = ErrorResponse.model_validate(response.json())
            code = detail.error.code
        except Exception:
            code = {
                401: ErrorCode.UNAUTHENTICATED,
                403: ErrorCode.FORBIDDEN,
                404: ErrorCode.NOT_FOUND,
                429: ErrorCode.QUOTA_EXCEEDED,
            }.get(response.status_code, ErrorCode.RAG_UNAVAILABLE)
        return RagError(response.status_code, code)
