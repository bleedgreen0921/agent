"""Reusable clients with request-local settings and explicit stream ownership."""
import asyncio
from contextlib import asynccontextmanager
import httpx
from execution.infra.gate import CapacityGate
from execution.infra.types import CapacityTimeout
from execution.flow import DownstreamGovernor, DownstreamLimited
from execution.persistence import using
from contextvars import ContextVar

fail_fast_pressure = ContextVar("fail_fast_pressure", default=False)

SERVICES = frozenset({'agent-model', 'rag', 'embedding', 'rewrite', 'rerank'})


class AsyncHttp:
    def __init__(self, runtime, transports=None):
        self.runtime = runtime
        self.clients, self.gates = {}, {}
        self.sdk_clients = {}
        self.governor = DownstreamGovernor()
        self.transports = transports or {}

    async def register_config(self):
        import os
        configured = [('agent-model', 'AGENT_MODEL_URL'), ('rag', 'RAG_BASE_URL'),
                      ('embedding', 'AGENT_EMBEDDING_URL' if os.environ.get('AGENT_EMBEDDING_URL') else 'RAG_EMBEDDING_URL')] if self.runtime.role.startswith('agent') else [
                      ('embedding', 'RAG_EMBEDDING_URL'), ('rewrite', 'RAG_REWRITE_URL'), ('rerank', 'RAG_RERANK_URL')]
        with using(self.runtime):
            for service, env in configured:
                if os.environ.get(env): await self.governor.register(service, os.environ[env])

    async def register_available(self):
        import logging
        import psycopg
        from execution.infra.types import ResourceError, OperationDeadline
        from execution.persistence import bind
        try:
            with bind(self.runtime, deadline=OperationDeadline.after(2)):
                await self.register_config()
        except (ResourceError, psycopg.Error, TimeoutError):
            logging.getLogger(__name__).warning('Flow policy registration unavailable; database admission remains required')

    def _service(self, service):
        self.runtime.check()
        if service not in SERVICES:
            raise ValueError('Unknown HTTP logical service')
        if service not in self.clients:
            config = self.runtime.config
            self.clients[service] = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=config.http_connections, max_keepalive_connections=config.http_keepalive),
                trust_env=False, follow_redirects=False, transport=self.transports.get(service))
            self.gates[service] = CapacityGate(self.runtime, config.http_concurrency, config.http_pending)
        return self.clients[service], self.gates[service]

    @asynccontextmanager
    async def borrow(self, service, url, deadline):
        from execution.progress import activity
        async with activity('http:' + service, deadline) as phase_id:
            async with self._borrow(service, url, deadline, phase_id) as value:
                yield value

    @asynccontextmanager
    async def _borrow(self, service, url, deadline, phase_id):
        client, gate = self._service(service)
        while True:
            from execution.timing import span
            borrowed = gate.borrow(deadline)
            with span('http_capacity_wait','wait'):
                await borrowed.__aenter__()
            try:
                with using(self.runtime):
                    with span('rate_admission','control'):
                        permit, delay = await self.governor.acquire(service, url, deadline.remaining())
                if permit:
                    from execution.progress import ProgressStore
                    from execution.scope import current_attempt
                    if phase_id: await ProgressStore().waiting(current_attempt(), phase_id, None)
                    yield client, permit
                    return
            finally:
                await borrowed.__aexit__(None,None,None)
            from execution.progress import ProgressStore
            from execution.scope import current_attempt
            if phase_id: await ProgressStore().waiting(current_attempt(), phase_id, getattr(delay, 'reason', 'downstream_rate'))
            cooling = getattr(delay, 'reason', None) in {'downstream_cooldown', 'downstream_probe'}
            if fail_fast_pressure.get() and cooling or delay >= deadline.remaining():
                raise DownstreamLimited(delay)
            async with asyncio.timeout_at(deadline.when):
                with span('rate_cooldown_wait','wait'):
                    await asyncio.sleep(min(delay, 1.))

    async def feedback(self, permit, **kwargs):
        from execution.persistence import bind, deadline, call_scope
        # Feedback describes an already dispatched request. It gets a small
        # control budget after a request timeout, still within the attempt/exit
        # budget, and never admits another external operation.
        with using(self.runtime), call_scope(None):
            limit = deadline(3)
            if self.runtime._closing_deadline:
                from execution.infra.types import OperationDeadline
                limit = OperationDeadline(min(limit.when, self.runtime._closing_deadline.when))
        with bind(self.runtime, deadline=limit, cleanup=not self.runtime.accepting), call_scope(None):
            await self.governor.feedback(permit, **kwargs)

    @asynccontextmanager
    async def stream(self, service, method, url, *, deadline, **kwargs):
        response = None
        accounting = None
        dispatched = False
        async with self.runtime.operation(), self.borrow(service, url, deadline) as (client, permit):
            reported = False
            try:
                async with asyncio.timeout_at(deadline.when):
                    request_budget = kwargs.pop('request_budget', False)
                    auth = kwargs.pop('auth', httpx.USE_CLIENT_DEFAULT)
                    if deadline.remaining() <= 0:
                        raise CapacityTimeout('HTTP operation deadline expired before dispatch')
                    from execution.scope import authorize
                    await authorize()
                    kwargs.setdefault('timeout', httpx.Timeout(max(.001, deadline.remaining())))
                    if request_budget:
                        kwargs['headers'] = {**kwargs.get('headers', {}),
                            'X-Request-Budget-Seconds': str(max(.001, deadline.remaining()))}
                    request = client.build_request(method, url, **kwargs)
                    from execution import http_accounting
                    accounting = await http_accounting.prepare(service,request)
                    if accounting: request=accounting[0]
                    await authorize()
                    await http_accounting.sent(accounting)
                    dispatched=True
                    from execution.timing import span
                    with span('http_exchange','execute'):
                        response = await client.send(request, stream=True, follow_redirects=False, auth=auth)
                        if accounting:
                            await response.aread()
                    await http_accounting.complete(accounting,response)
                    accounting=None
                    if response.is_error:
                        await self.feedback(permit, status=response.status_code, retry_after=response.headers.get('Retry-After'))
                        reported = True
                    yield response
                    if not reported:
                        await self.feedback(permit, status=response.status_code)
                        reported = True
            except BaseException as exc:
                if accounting:
                    await http_accounting.failed(accounting,exc,dispatched=dispatched)
                if isinstance(exc,(httpx.TransportError,TimeoutError)) and not reported:
                    await self.feedback(permit, transport_error=True)
                raise
            finally:
                if response is not None:
                    await self.runtime.cleanup(response.aclose())

    async def request(self, service, method, url, *, deadline, **kwargs):
        async with self.stream(service, method, url, deadline=deadline, **kwargs) as response:
            await response.aread()
            return response

    def quiesce(self):
        for gate in self.gates.values():
            gate.wake()

    async def aclose(self):
        for client in self.sdk_clients.values():
            await client.aclose()
        for client in reversed(list(self.clients.values())):
            await client.aclose()

    def sdk_client(self, service):
        self.runtime.check()
        if service not in self.sdk_clients:
            self.sdk_clients[service] = ManagedSdkClient(self, service)
        return self.sdk_clients[service]

    def snapshot(self):
        return {name: gate.snapshot() for name, gate in self.gates.items()}


class ManagedSdkClient(httpx.AsyncClient):
    """SDK type-compatible facade. All actual sockets belong to AsyncHttp."""
    def __init__(self, manager, service):
        def forbidden(request):
            raise RuntimeError('SDK facade has no independent transport')
        super().__init__(transport=httpx.MockTransport(forbidden), trust_env=False, follow_redirects=False)
        self.manager, self.service = manager, service

    async def send(self, request, *, stream=False, auth=httpx.USE_CLIENT_DEFAULT, follow_redirects=httpx.USE_CLIENT_DEFAULT):
        from execution.scope import operation_deadline, authorize
        from execution.persistence import current_call_deadline
        limit = current_call_deadline() or await operation_deadline(30)
        operation = self.manager.runtime.operation()
        borrow = self.manager.borrow(self.service, request.url, limit)
        await operation.__aenter__()
        borrowed = False
        accounting = None
        dispatched = False
        try:
            client, permit = await borrow.__aenter__(); borrowed = True
            async with asyncio.timeout_at(limit.when):
                await authorize()
                from execution import http_accounting
                accounting=await http_accounting.prepare(self.service,request)
                if accounting: request=accounting[0]
                await authorize()
                await http_accounting.sent(accounting)
                request.extensions['timeout'] = httpx.Timeout(max(.001, limit.remaining())).as_dict()
                dispatched=True
                from execution.timing import span
                with span('http_send','execute'):
                    response = await client.send(request, stream=True, auth=auth, follow_redirects=False)
                if response.is_error:
                    await self.manager.feedback(permit, status=response.status_code, retry_after=response.headers.get("Retry-After"))
        except BaseException as exc:
            if accounting:
                await http_accounting.failed(accounting,exc,dispatched=dispatched)
            try:
                if borrowed and isinstance(exc, (httpx.TransportError, TimeoutError)):
                    await self.manager.feedback(permit, transport_error=True)
            finally:
                try:
                    if borrowed: await borrow.__aexit__(type(exc), exc, exc.__traceback__)
                finally: await operation.__aexit__(None,None,None)
            raise
        original_close = response.aclose
        closed = False
        completed_once = False
        async def close():
            nonlocal closed
            if closed: return
            closed = True
            async def release():
                try: await original_close()
                finally:
                    try: await borrow.__aexit__(None,None,None)
                    finally: await operation.__aexit__(None,None,None)
            await self.manager.runtime.cleanup(release())
        response.aclose = close
        async def completed(error=False):
            nonlocal completed_once
            if completed_once:return
            completed_once=True
            if accounting:
                if error: await http_accounting.failed(accounting,TimeoutError(),dispatched=True)
                else: await http_accounting.complete(accounting,response)
            if not response.is_error:
                await self.manager.feedback(permit, transport_error=True) if error else await self.manager.feedback(permit, status=response.status_code)
        response.stream = DeadlineStream(response.stream, limit, close, completed,response=response)
        if hasattr(response,'_content'):
            await completed()
            await close()
        if not stream:
            try:
                async with asyncio.timeout_at(limit.when): await response.aread()
            finally: await close()
        return response


class DeadlineStream(httpx.AsyncByteStream):
    """Streaming SDK reads retain both deadline and HTTP capacity ownership."""
    def __init__(self, stream, deadline, close, completed=None, response=None):
        self.stream, self.deadline, self.close = stream, deadline, close
        self.completed = completed
        self.response=response

    async def __aiter__(self):
        chunks=[]; size=0
        try:
            async with asyncio.timeout_at(self.deadline.when):
                async for chunk in self.stream:
                    size+=len(chunk)
                    if size > 8*1024*1024: raise ValueError('Provider response exceeds diagnostic response limit')
                    chunks.append(chunk)
                    yield chunk
                if self.response is not None: self.response._content=b''.join(chunks)
                if self.completed: await self.completed()
        except BaseException:
            if self.completed: await self.completed(True)
            raise
        finally:
            await self.close()

    async def aclose(self):
        await self.stream.aclose()
