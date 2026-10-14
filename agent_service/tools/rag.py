"""RAG evidence tools supplied through the generic Agent tool provider API."""
import json
import os
from langchain.tools import ToolRuntime, tool
from agent_service.rag_client import RagClient, RagError
from agent_service.tooling import FatalToolError, ToolDeclaration, ToolExecutionContext, ToolProvider
from contracts.v1 import ErrorCode, EvidenceSearchRequest

def fatal_error(exc: RagError) -> FatalToolError | None:
    if exc.code == ErrorCode.UNAUTHENTICATED:
        return FatalToolError('AUTH_FAILED', str(exc.code))
    if exc.code == ErrorCode.QUOTA_EXCEEDED:
        return FatalToolError('QUOTA_EXCEEDED', str(exc.code))
    return None
from execution.contracts import ToolOutcome, CONTRACT_VERSION

def outcome(content, *, evidence=(), notices=(), **metadata):
    return (content, ToolOutcome(content, tuple(evidence), tuple(notices), metadata).to_artifact())

@tool(response_format='content_and_artifact')
async def search_evidence(query: str, runtime: ToolRuntime[ToolExecutionContext], top_k: int=5) -> tuple[str, dict]:
    """Search evidence relevant to a query."""
    context = runtime.context
    client = RagClient(os.environ['RAG_BASE_URL'], os.environ['RAG_SERVICE_TOKEN'], timeout_seconds=context.rag_timeout_seconds)
    try:
        response = await client.search(context.run_id, str(context.business_call_id()), EvidenceSearchRequest(query=query, top_k=top_k))
        notices = []
        if response.status == 'no_hits':
            notices.append({'code': 'RAG_NO_HITS', 'message': 'The knowledge base returned no matching evidence.'})
        if response.degradations:
            notices.append({'code': 'RAG_DEGRADED', 'message': 'Knowledge retrieval completed with degraded components.'})
        return outcome(response.model_dump_json(exclude_none=True), evidence=[item.model_dump(exclude_none=True) for item in response.evidences], notices=notices, result_summary={'status': response.status, 'evidence_count': len(response.evidences), 'degradations': response.degradations}, service_request_id=response.request_id, retrieval_id=response.retrieval_id, evidence_ids=[item.evidence_id for item in response.evidences])
    except RagError as exc:
        fatal = fatal_error(exc)
        if fatal:
            raise fatal from exc
        unavailable = exc.code in {ErrorCode.RAG_UNAVAILABLE, ErrorCode.RAG_TIMEOUT}
        return outcome(json.dumps({'status': 'unavailable', 'evidences': []} if unavailable else {'status': 'error', 'code': str(exc.code)}), notices=[{'code': 'RAG_UNAVAILABLE', 'message': 'Knowledge retrieval is temporarily unavailable.'}] if unavailable else [], status='degraded' if unavailable else 'failed', error_code=str(exc.code))


@tool(response_format='content_and_artifact')
async def read_evidence(evidence_id: str, runtime: ToolRuntime[ToolExecutionContext]) -> tuple[str, dict]:
    """Read one evidence fragment."""
    context = runtime.context
    client = RagClient(os.environ['RAG_BASE_URL'], os.environ['RAG_SERVICE_TOKEN'], timeout_seconds=context.rag_timeout_seconds)
    try:
        item = await client.read(context.run_id, str(context.business_call_id()), evidence_id)
        return outcome(item.model_dump_json(exclude_none=True), evidence=[item.model_dump(exclude_none=True)], result_summary={'status': 'ok', 'evidence_count': 1}, evidence_ids=[item.evidence_id])
    except RagError as exc:
        fatal = fatal_error(exc)
        if fatal:
            raise fatal from exc
        return outcome(json.dumps({'status': 'error', 'code': str(exc.code)}), status='failed', error_code=str(exc.code))


def tools():
    return ToolProvider(name='rag_evidence', version='2', contract_version=CONTRACT_VERSION, tools=(ToolDeclaration(search_evidence, '1', 'read_only', True), ToolDeclaration(read_evidence, '1', 'read_only', True)))
