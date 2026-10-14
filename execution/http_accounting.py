"""Physical provider dispatch accounting, used by both native and SDK HTTP."""
import json
from uuid import uuid4
from execution import tokens, archive
from execution.scope import work_owner

SERVICES={'agent-model','embedding','rewrite','rerank'}


async def prepare(service, request):
    from execution.delegation import dispatch_allocation
    if service=='rag' and request.method=='POST' and request.url.path.endswith('/evidence/search') and dispatch_allocation.get():
        return request,dispatch_allocation.get(),None,'rag-delegation'
    if service not in SERVICES or request.method != 'POST' or not request.content:return None
    body=json.loads(request.content)
    value=await tokens.envelope(service,body)
    # SDK request construction has already occurred: replace only the bounded
    # generation parameter, and rebuild headers consistently.
    import httpx
    bounded=httpx.Request(request.method,request.url,headers={k:v for k,v in request.headers.items() if k.lower()!='content-length'},
        json=value.body,extensions=request.extensions)
    identity=uuid4(); owner=work_owner()
    handle=await tokens.reserve(identity,value,parent=owner.call_id if owner else None)
    try:
        saved=await archive.prepare(service,{'method':request.method,'endpoint':request.url.path,'body':value.body,
            'input_tokens':value.input_tokens,'output_reserved':value.output_tokens,'estimation':value.estimation},
            owner.call_id if owner else identity)
    except BaseException:
        await tokens.settle(handle,not_sent=True)
        raise
    return bounded,handle,saved,service


async def sent(value):
    if value:await tokens.sent(value[1])


async def complete(value, response):
    if not value:return
    if value[3]=='rag-delegation':return  # Typed RAG receipt settles the finite allocation.
    try:payload=response.json()
    except ValueError:payload={}
    payload_archive={'status_code':response.status_code,'body':response.text,
        'headers':{k:response.headers[k] for k in ('content-type','x-request-id','retry-after') if k in response.headers}}
    try:response.content.decode('utf-8',errors='strict')
    except UnicodeDecodeError:
        import base64
        payload_archive['body_base64']=base64.b64encode(response.content).decode()
    await archive.finish(value[2],payload_archive)
    await tokens.settle(value[1],tokens.usage(payload,value[3]))


async def failed(value, error, *, dispatched):
    if not value:return
    # Never mask cancellation or loss of authority with diagnostic cleanup.
    import logging
    try:
        if value[2] is not None:await archive.finish(value[2],error=error)
        await tokens.settle(value[1],not_sent=not dispatched)
    except BaseException:
        logging.getLogger(__name__).warning('Call diagnostics incomplete; token reservation retained')
