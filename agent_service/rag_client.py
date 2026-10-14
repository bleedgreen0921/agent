"""HTTP-only adapter for Agent calls to the RAG evidence API."""
from dataclasses import dataclass
from uuid import UUID
import httpx
from execution.flow import DownstreamLimited
from contracts.v1 import ErrorCode, ErrorResponse, Evidence, EvidenceSearchRequest, EvidenceSearchResponse
from agent_service.timeouts import configured_timeouts, positive_seconds
from execution.scope import call_timeout, operation_deadline
from execution.persistence import resources

@dataclass(frozen=True)
class TrustedRunContext:
    run_id: UUID

    @classmethod
    async def from_persisted_run(cls, run_id: UUID) -> 'TrustedRunContext':
        from execution.stores import stores
        return cls(await stores()[1].run_identity(run_id))

class RagError(Exception):

    def __init__(self, status: int, code: ErrorCode):
        self.status = status
        self.code = code
        super().__init__(code)

class RagClient:

    def __init__(self, base_url: str, service_token: str, client=None, timeout_seconds: float | None=None):
        self.base_url = base_url.rstrip('/')
        self.service_token = service_token
        seconds = configured_timeouts().rag_seconds if timeout_seconds is None else positive_seconds(timeout_seconds, 'AGENT_RAG_TIMEOUT_SECONDS')
        self.timeout_seconds = seconds
        if client is not None:raise TypeError('RagClient uses the resource-owned HTTP transport')

    async def search(self, run_id: UUID, tool_call_id: str, request: EvidenceSearchRequest) -> EvidenceSearchResponse:
        context = await TrustedRunContext.from_persisted_run(run_id)
        seconds = await call_timeout(self.timeout_seconds)
        from execution import delegation
        ticket,allocation=await delegation.issue(run_id,tool_call_id,request.model_dump())
        allocation_key=delegation.dispatch_allocation.set(allocation)
        try:
            response = await resources().http.request('rag','POST',self.base_url + '/v1/evidence/search', deadline=await operation_deadline(seconds), request_budget=True, headers={'Authorization': 'Bearer ' + self.service_token, 'X-Run-Id': str(context.run_id), 'X-Tool-Call-Id': tool_call_id, 'X-Request-Budget-Seconds': str(seconds),**({'X-Token-Allocation':ticket} if ticket else {})}, json=request.model_dump())
        except DownstreamLimited as exc:
            await self._reconcile_allocation(allocation,context.run_id,tool_call_id)
            raise RagError(429, ErrorCode.RAG_RATE_LIMITED) from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            await self._reconcile_allocation(allocation,context.run_id,tool_call_id)
            raise RagError(503, ErrorCode.RAG_TIMEOUT) from exc
        except httpx.TransportError as exc:
            await self._reconcile_allocation(allocation,context.run_id,tool_call_id)
            raise RagError(503, ErrorCode.RAG_UNAVAILABLE) from exc
        finally:
            delegation.dispatch_allocation.reset(allocation_key)
        if response.is_error:
            await self._reconcile_allocation(allocation,context.run_id,tool_call_id)
            raise self._error(response)
        try:
            result=EvidenceSearchResponse.model_validate(response.json())
            await delegation.reconcile(allocation,result.usage.model_dump() if result.usage else None)
            return result
        except (TypeError, ValueError) as exc:
            await self._reconcile_allocation(allocation,context.run_id,tool_call_id)
            raise RagError(502, ErrorCode.RAG_UNAVAILABLE) from exc

    async def read(self, run_id: UUID, tool_call_id: str, evidence_id: str) -> Evidence:
        context = await TrustedRunContext.from_persisted_run(run_id)
        seconds = await call_timeout(self.timeout_seconds)
        try:
            response = await resources().http.request('rag','GET',self.base_url + '/v1/evidence/' + evidence_id, deadline=await operation_deadline(seconds), request_budget=True, headers={'Authorization': 'Bearer ' + self.service_token, 'X-Run-Id': str(context.run_id), 'X-Tool-Call-Id': tool_call_id, 'X-Request-Budget-Seconds': str(seconds)})
        except DownstreamLimited as exc:
            raise RagError(429, ErrorCode.RAG_RATE_LIMITED) from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise RagError(503, ErrorCode.RAG_TIMEOUT) from exc
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
            code = ErrorCode.RAG_RATE_LIMITED if response.status_code == 429 else detail.error.code
        except Exception:
            code = {401: ErrorCode.UNAUTHENTICATED, 403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND, 429: ErrorCode.RAG_RATE_LIMITED}.get(response.status_code, ErrorCode.RAG_UNAVAILABLE)
        return RagError(response.status_code, code)

    async def _reconcile_allocation(self,handle,run_id,tool_call_id):
        if handle is None:return
        from execution.persistence import call_scope
        from execution import delegation
        try:
            from execution import tokens
            try:
                await tokens.settle(handle,not_sent=True)
                return
            except ValueError:pass
            # One read-only status check. Never repeat the search or a model.
            with call_scope(None):
                response=await resources().http.request('rag','GET',self.base_url+'/v1/evidence/operations/'+str(handle[1]),
                    deadline=await operation_deadline(3),headers={'Authorization':'Bearer '+self.service_token,
                        'X-Run-Id':str(run_id),'X-Tool-Call-Id':tool_call_id})
            if not response.is_error:
                value=response.json()
                if value['status'] in ('completed','failed'):await delegation.reconcile(handle,value['result'].get('usage'))
        except (httpx.HTTPError,TimeoutError,ValueError,KeyError,DownstreamLimited):
            pass  # Unconfirmed allocations remain charged, not released.
