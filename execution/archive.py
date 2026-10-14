"""Exact business I/O archives, separate from execution/recovery authority."""
import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from execution.persistence import resources, transaction, deadline
from execution.scope import current_attempt, work_owner
from execution.retry import retry_transaction


def root():
    value=Path(os.environ.get('CALL_ARCHIVE_DIR','.data/call-archives')).resolve()
    value.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(value,0o700)
    return value


def write(path, payload):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str).encode()
    data=gzip.compress(raw,mtime=0)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as file:
            file.write(data); file.flush(); os.fsync(file.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def encode(payload,secrets):
    count=0
    sensitive={'authorization','proxy_authorization','api_key','apikey','password','secret',
               'access_token','refresh_token','lease_token','service_token'}
    secrets=sorted({s for s in secrets if s},key=len,reverse=True)
    def clean(value):
        nonlocal count
        if isinstance(value,dict):
            result={}
            for key,item in value.items():
                if str(key).lower().replace('-','_') in sensitive:
                    count+=1;result[key]='[redacted]'
                elif key=='body_base64' and isinstance(item,str):
                    import base64
                    raw=base64.b64decode(item,validate=True)
                    for secret in secrets:
                        encoded=secret.encode();count+=raw.count(encoded)
                        raw=raw.replace(encoded,b'[redacted]')
                    result[key]=base64.b64encode(raw).decode()
                else:result[key]=clean(item)
            return result
        if isinstance(value,(list,tuple)):return [clean(v) for v in value]
        if isinstance(value,str):
            # Tool arguments can contain a JSON document inside a message.
            try:nested=json.loads(value)
            except (ValueError,TypeError):nested=None
            if isinstance(nested,(dict,list)):
                replacement=clean(nested)
                if replacement!=nested:value=json.dumps(replacement,ensure_ascii=False,sort_keys=True)
            for secret in secrets:
                count+=value.count(secret);value=value.replace(secret,'[redacted]')
        return value
    sanitized=clean(payload)
    return json.dumps({'redactions':count,'data':sanitized},ensure_ascii=False,sort_keys=True,default=str).encode()


def credential_values():
    from urllib.parse import urlsplit,unquote
    values=[]
    for name in ('RAG_SERVICE_TOKEN','AGENT_MODEL_KEY','AGENT_EMBEDDING_KEY','RAG_EMBEDDING_KEY','RAG_REWRITE_KEY','RAG_RERANK_KEY','OPENAI_API_KEY'):
        value=os.environ.get(name,'')
        if len(value)>=12:values.append(value)
    for name in ('AGENT_DATABASE_URL','RAG_DATABASE_URL','MIGRATION_DATABASE_URL','MAINTENANCE_DATABASE_URL'):
        value=os.environ.get(name,'')
        if value:
            values.append(value)
            password=unquote(urlsplit(value).password or '')
            if password:values.append(password)
    settings=getattr(resources(),'agent_settings',None)
    if settings and len(settings.get('key',''))>=12:values.append(settings['key'])
    attempt=current_attempt()
    if attempt:values.append(str(attempt.token))
    return values


async def prepare(service, payload, call_id=None):
    runtime=resources(); namespace='agent' if runtime.role.startswith('agent') else 'rag'
    identity=uuid4(); attempt=current_attempt()
    from execution.tokens import budget_context
    context=budget_context.get() or {}
    related=context.get('run_id',str(attempt.task.id) if attempt else context.get('task_id'))
    payload={'context':{'run_id':str(related) if related else None,'call_id':str(call_id) if call_id else None,
                        'budget_id':context.get('id'),'tool_call_id':context.get('tool_call_id')},'request':payload}
    base=await runtime.io.run(root,owner=work_owner(),deadline=deadline(10))
    path=str(base/f'{namespace}-{identity}-input.json.gz')
    raw=await runtime.io.run(encode,payload,credential_values(),owner=work_owner(),deadline=deadline(10))
    digest=hashlib.sha256(raw).hexdigest()
    await _insert(namespace,identity,attempt.task.id if attempt else None,call_id,str(related) if related else None,service,path,digest)
    await runtime.io.run(write,path,raw,owner=work_owner(),deadline=deadline(10))
    return namespace,identity,path


@retry_transaction
async def _insert(namespace,identity,task_id,call_id,related,service,path,digest):
    async with transaction() as conn:
        await conn.execute('INSERT INTO execution.call_archives(namespace,id,task_id,call_id,run_reference,service,input_path,input_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                           (namespace,identity,task_id,call_id,related,service,path,digest))


async def finish(handle, payload=None, *, error=None):
    namespace,identity,input_path=handle
    path=input_path.replace('-input.json.gz','-output.json.gz') if payload is not None else None
    raw=await resources().io.run(encode,payload,credential_values(),owner=work_owner(),deadline=deadline(10)) if path else None
    digest=await resources().io.run(write,path,raw,owner=work_owner(),deadline=deadline(10)) if path else None
    await _finish(namespace,identity,path,digest,error)


@retry_transaction
async def _finish(namespace,identity,path,digest,error):
    async with transaction() as conn:
        await conn.execute('UPDATE execution.call_archives SET output_path=%s,output_sha256=%s,status=%s,error_type=%s WHERE namespace=%s AND id=%s',
                           (path,digest,'complete' if path else 'incomplete',type(error).__name__ if error else None,namespace,identity))
