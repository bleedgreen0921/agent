"""HTTP-only RAG adapter. Business routes are enabled in a later phase."""

from dataclasses import dataclass
from uuid import UUID

import httpx

from contracts.v1 import ErrorCode, ErrorResponse, EvidenceSearchRequest, EvidenceSearchResponse
from db.connection import connect


@dataclass(frozen=True)
class TrustedRunContext:
    run_id: UUID
    team_id: UUID

    @classmethod
    def from_persisted_run(cls, run_id: UUID) -> "TrustedRunContext":
        with connect("AGENT_DATABASE_URL") as conn:
            row = conn.execute("SELECT id, team_id FROM agent.runs WHERE id = %s", (run_id,)).fetchone()
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
        self.client = client or httpx.Client(timeout=30)

    def search(self, run_id: UUID, tool_call_id: str, request: EvidenceSearchRequest) -> EvidenceSearchResponse:
        context = TrustedRunContext.from_persisted_run(run_id)
        response = self.client.post(
            self.base_url + "/v1/evidence/search",
            headers={"Authorization": "Bearer " + self.service_token, "X-Team-Id": str(context.team_id), "X-Run-Id": str(context.run_id), "X-Tool-Call-Id": tool_call_id},
            json=request.model_dump(),
        )
        if response.is_error:
            detail = ErrorResponse.model_validate(response.json())
            raise RagError(response.status_code, detail.error.code)
        return EvidenceSearchResponse.model_validate(response.json())
