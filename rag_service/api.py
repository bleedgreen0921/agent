from uuid import UUID
from fastapi import APIRouter, Request, Depends, File, Form, Header, UploadFile
from contracts.errors import ApiError
from contracts.v1 import DocumentDeleted, DocumentVersionRef, DocumentVersionStatus, ErrorCode, Evidence, EvidenceSearchRequest, EvidenceSearchResponse, WorkerSnapshot
from db.async_queries import worker_snapshot
from execution.infra.types import ResourceError
import psycopg
import asyncio
import os
import math
from execution.persistence import bind, resources
from execution.infra.types import OperationDeadline
from rag_service.documents import add_version, create_document, soft_delete, version_status
from rag_service.retrieval import read_evidence, search
from rag_service.service_auth import require_service
document_router = APIRouter(prefix='/v1/documents', tags=['documents'])
worker_router = APIRouter(prefix='/v1/workers', tags=['workers'])
evidence_router = APIRouter(prefix='/v1/evidence', tags=['evidence'], dependencies=[Depends(require_service)])

@worker_router.get('', response_model=WorkerSnapshot)
async def worker_status(request: Request):
    try:
        return await worker_snapshot(request.app.state.resources, 'rag')
    except (ResourceError, psycopg.Error, OSError, TimeoutError):
        raise ApiError(503, ErrorCode.INTERNAL_ERROR, 'Worker status unavailable') from None

@document_router.post('', status_code=202, response_model=DocumentVersionRef)
async def upload_document(file: UploadFile=File(...), title: str=Form(...), idempotency_key: str | None=Header(default=None, alias='Idempotency-Key')):
    return await create_document(file, title, idempotency_key)

@document_router.post('/{document_id}/versions', status_code=202, response_model=DocumentVersionRef)
async def upload_version(document_id: UUID, file: UploadFile=File(...), idempotency_key: str | None=Header(default=None, alias='Idempotency-Key')):
    return await add_version(document_id, file, idempotency_key)

@document_router.get('/{document_id}/versions/{version_id}', response_model=DocumentVersionStatus)
async def get_version(document_id: UUID, version_id: UUID):
    return await version_status(document_id, version_id)

@document_router.delete('/{document_id}', response_model=DocumentDeleted)
async def delete_document(document_id: UUID):
    return await soft_delete(document_id)

async def trusted_context(x_run_id: str=Header(alias='X-Run-Id'), x_tool_call_id: str=Header(alias='X-Tool-Call-Id')) -> tuple[str, str]:
    if not x_run_id.strip() or not x_tool_call_id.strip():
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Missing trusted context')
    return (x_run_id, x_tool_call_id)

@evidence_router.post('/search', response_model=EvidenceSearchResponse, response_model_exclude_none=True)
async def search_evidence(body: EvidenceSearchRequest, context: tuple[str, str]=Depends(trusted_context), request_budget: float | None=Header(default=None,alias='X-Request-Budget-Seconds'), token_allocation: str | None=Header(default=None,alias='X-Token-Allocation')):
    configured=float(os.environ.get('RAG_SEARCH_TIMEOUT_SECONDS','120'))
    if not math.isfinite(configured) or configured<=0: raise RuntimeError('Invalid RAG_SEARCH_TIMEOUT_SECONDS')
    if request_budget is not None and (not math.isfinite(request_budget) or request_budget<=0): raise ApiError(422,ErrorCode.INVALID_REQUEST,'Invalid request budget')
    limit=OperationDeadline.after(min(configured,request_budget) if request_budget is not None else configured)
    try:
        with bind(resources(),deadline=limit):
            from execution.delegation import decode
            allocation=decode(token_allocation,*context,body.model_dump()) if token_allocation else None
            from execution.admission import AdmissionStore
            async with AdmissionStore().request('search', limit), asyncio.timeout_at(limit.when):
                return await search(body,*context,**({'allocation':allocation} if allocation else {}))
    except TimeoutError:
        raise ApiError(503,ErrorCode.RAG_TIMEOUT,'Search exceeded its request budget') from None

@evidence_router.get('/operations/{operation_id}')
async def search_operation(operation_id: UUID, context: tuple[str,str]=Depends(trusted_context)):
    from execution.delegation import status
    return await status(operation_id,*context)

@evidence_router.get('/{evidence_id}', response_model=Evidence, response_model_exclude_none=True)
async def get_evidence(evidence_id: str, context: tuple[str, str]=Depends(trusted_context)):
    return await read_evidence(evidence_id, *context)
