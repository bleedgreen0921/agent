"""Acquire upload capacity before Starlette parses/spools multipart bodies."""
import asyncio
import logging
from contextvars import ContextVar
from uuid import uuid4
import psycopg
from starlette.responses import JSONResponse
from contracts.errors import ApiError
from execution.admission import AdmissionStore
from execution.infra.types import OperationDeadline, ResourceError
from execution.persistence import bind, resources, PersistenceOutcomeUnknown

upload_admitted = ContextVar('upload_admitted', default=False)
log = logging.getLogger(__name__)


class UploadAdmission:
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') != 'POST' or not scope.get('path','').startswith('/v1/documents'):
            return await self.app(scope,receive,send)
        started = False
        async def tracked_send(message):
            nonlocal started
            if message['type']=='http.response.start': started=True
            await send(message)
        try:
            limit = OperationDeadline.after(120)
            with bind(resources(),deadline=limit):
                async with AdmissionStore().request('upload',limit), asyncio.timeout_at(limit.when):
                    marker=upload_admitted.set(True)
                    try: await self.app(scope,receive,tracked_send)
                    finally: upload_admitted.reset(marker)
        except (ApiError,ResourceError,psycopg.Error,PersistenceOutcomeUnknown,TimeoutError) as exc:
            if started:
                log.warning('Upload cleanup unavailable; request permit retains its fixed expiry')
                return
            request_id='req_'+uuid4().hex
            code=exc.code if isinstance(exc,ApiError) else 'SERVICE_UNAVAILABLE'
            message=exc.message if isinstance(exc,ApiError) else 'Upload admission unavailable or timed out'
            response=JSONResponse({'error':{'code':code,'message':message},'request_id':request_id},
                status_code=exc.status if isinstance(exc,ApiError) else 503,
                headers={**((exc.headers or {}) if isinstance(exc,ApiError) else {'Retry-After':'5'}),'X-Request-Id':request_id})
            await response(scope,receive,send)
