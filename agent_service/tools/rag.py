"""RAG evidence tools supplied through the generic Agent tool provider API."""

import json
import os

from langchain.tools import ToolRuntime, tool

from agent_service.rag_client import RagClient, RagError
from agent_service.tooling import FatalToolError, ToolExecutionContext
from contracts.v1 import ErrorCode, EvidenceSearchRequest


def fatal_error(exc: RagError) -> FatalToolError | None:
    if exc.code == ErrorCode.UNAUTHENTICATED:
        return FatalToolError("AUTH_FAILED", str(exc.code))
    if exc.code in {ErrorCode.FORBIDDEN, ErrorCode.ACCESS_DENIED}:
        return FatalToolError("ACCESS_DENIED", str(exc.code))
    if exc.code == ErrorCode.QUOTA_EXCEEDED:
        return FatalToolError("QUOTA_EXCEEDED", str(exc.code))
    return None


@tool
def search_evidence(query: str, runtime: ToolRuntime[ToolExecutionContext], top_k: int = 5) -> str:
    """Search the configured evidence service for material relevant to a query."""
    context = runtime.context
    client = RagClient(os.environ["RAG_BASE_URL"], os.environ["RAG_SERVICE_TOKEN"])
    try:
        response = client.search(context.run_id, str(context.business_call_id(runtime.tool_call_id)), EvidenceSearchRequest(query=query, top_k=top_k))
        for item in response.evidences:
            context.evidence[item.evidence_id] = item.model_dump(exclude_none=True)
        if response.status == "no_hits":
            context.notices.append({"code": "RAG_NO_HITS", "message": "The knowledge base returned no matching evidence."})
        if response.degradations:
            context.notices.append({"code": "RAG_DEGRADED", "message": "Knowledge retrieval completed with degraded components."})
        context.add_tool_metadata(
            runtime.tool_call_id,
            result_summary={"status": response.status, "evidence_count": len(response.evidences), "degradations": response.degradations},
            service_request_id=response.request_id,
            retrieval_id=response.retrieval_id,
            evidence_ids=[item.evidence_id for item in response.evidences],
        )
        return response.model_dump_json(exclude_none=True)
    except RagError as exc:
        fatal = fatal_error(exc)
        if fatal:
            raise fatal from exc
        if exc.code == ErrorCode.RAG_UNAVAILABLE:
            context.notices.append({"code": "RAG_UNAVAILABLE", "message": "Knowledge retrieval is temporarily unavailable; contact an administrator if it persists."})
            context.add_tool_metadata(runtime.tool_call_id, status="degraded", result_summary={"status": "unavailable"}, error_code=str(exc.code))
            return json.dumps({"status": "unavailable", "evidences": []})
        context.add_tool_metadata(runtime.tool_call_id, status="failed", result_summary={"status": "error"}, error_code=str(exc.code))
        return json.dumps({"status": "error", "code": str(exc.code)})
    finally:
        client.client.close()


@tool
def read_evidence(evidence_id: str, runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Read one evidence fragment from the configured evidence service."""
    context = runtime.context
    client = RagClient(os.environ["RAG_BASE_URL"], os.environ["RAG_SERVICE_TOKEN"])
    try:
        item = client.read(context.run_id, str(context.business_call_id(runtime.tool_call_id)), evidence_id)
        context.evidence[item.evidence_id] = item.model_dump(exclude_none=True)
        context.add_tool_metadata(runtime.tool_call_id, result_summary={"status": "ok", "evidence_count": 1}, evidence_ids=[item.evidence_id])
        return item.model_dump_json(exclude_none=True)
    except RagError as exc:
        fatal = fatal_error(exc)
        if fatal:
            raise fatal from exc
        context.add_tool_metadata(runtime.tool_call_id, status="failed", result_summary={"status": "error"}, error_code=str(exc.code))
        return json.dumps({"status": "error", "code": str(exc.code)})
    finally:
        client.client.close()


def tools():
    return [search_evidence, read_evidence]
