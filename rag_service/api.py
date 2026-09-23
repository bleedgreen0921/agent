import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile

from contracts.errors import ApiError
from contracts.v1 import DocumentAccess, DocumentDeleted, DocumentVersionRef, DocumentVersionStatus, ErrorCode, Evidence, EvidenceSearchRequest, EvidenceSearchResponse, StrictModel
from identity.security import Principal, admin
from rag_service.documents import add_version, create_document, set_access, soft_delete, version_status
from rag_service.retrieval import read_evidence, search
from rag_service.service_auth import require_service


admin_router = APIRouter(prefix="/v1/admin/documents", tags=["documents"], dependencies=[Depends(admin)])
evidence_router = APIRouter(prefix="/v1/evidence", tags=["evidence"], dependencies=[Depends(require_service)])


def parse_team_ids(raw: str) -> list[UUID]:
    try:
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError
        return [UUID(item) for item in value]
    except (ValueError, TypeError, AttributeError):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid team_ids") from None


class AccessRequest(StrictModel):
    visibility: str
    team_ids: list[UUID]


@admin_router.post("", status_code=202, response_model=DocumentVersionRef)
def upload_document(file: UploadFile = File(...), title: str = Form(...), visibility: str = Form(...), team_ids: str = Form("[]"), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(admin)):
    return create_document(file, title, visibility, parse_team_ids(team_ids), idempotency_key, principal.key_id)


@admin_router.post("/{document_id}/versions", status_code=202, response_model=DocumentVersionRef)
def upload_version(document_id: UUID, file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), principal: Principal = Depends(admin)):
    return add_version(document_id, file, idempotency_key, principal.key_id)


@admin_router.get("/{document_id}/versions/{version_id}", response_model=DocumentVersionStatus)
def get_version(document_id: UUID, version_id: UUID):
    return version_status(document_id, version_id)


@admin_router.put("/{document_id}/access", response_model=DocumentAccess)
def change_access(document_id: UUID, body: AccessRequest):
    return set_access(document_id, body.visibility, body.team_ids)


@admin_router.delete("/{document_id}", response_model=DocumentDeleted)
def delete_document(document_id: UUID):
    return soft_delete(document_id)


def trusted_context(x_team_id: UUID = Header(alias="X-Team-Id"), x_run_id: str = Header(alias="X-Run-Id"), x_tool_call_id: str = Header(alias="X-Tool-Call-Id")) -> tuple[UUID, str, str]:
    if not x_run_id or not x_tool_call_id:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Missing trusted context")
    return x_team_id, x_run_id, x_tool_call_id


@evidence_router.post("/search", response_model=EvidenceSearchResponse, response_model_exclude_none=True)
def search_evidence(body: EvidenceSearchRequest, context: tuple[UUID, str, str] = Depends(trusted_context)):
    return search(body, *context)


@evidence_router.get("/{evidence_id}", response_model=Evidence, response_model_exclude_none=True)
def get_evidence(evidence_id: str, context: tuple[UUID, str, str] = Depends(trusted_context)):
    return read_evidence(evidence_id, *context)
