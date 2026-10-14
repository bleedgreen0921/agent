"""Immutable data dependencies. Diagnostics are never used as recovery data."""
import hashlib
import json
from uuid import UUID
from execution.persistence import transaction, resources, deadline
from execution.scope import work_owner
from execution.retry import retry_transaction
from execution.contracts import TaskRef, TaskKind
from execution import store

REF='_execution_body'
EVIDENCE_REF='_evidence_body'


def reference(run_id,digest,format,length):
    return {REF:{'run_id':str(run_id),'sha256':digest,'format':format,'bytes':length,'version':1}}


@retry_transaction
async def put_bytes(run_id,token,format,data):
    digest=hashlib.sha256(format.encode()+b'\0'+data).hexdigest()
    async with transaction('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn,TaskRef(run_id,TaskKind.AGENT),token)
        await conn.execute('INSERT INTO agent.execution_bodies(run_id,sha256,format,body,byte_length) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                           (run_id,digest,format,data,len(data)))
        row=await (await conn.execute('SELECT format,body,byte_length FROM agent.execution_bodies WHERE run_id=%s AND sha256=%s',(run_id,digest))).fetchone()
        if row['format']!=format or bytes(row['body'])!=data:raise ValueError('Immutable body conflict')
        await conn.execute("INSERT INTO agent.body_pins(run_id,sha256,owner) VALUES (%s,%s,'recovery') ON CONFLICT DO NOTHING",(run_id,digest))
        await store.assert_active(conn,TaskRef(run_id,TaskKind.AGENT),token)
    return reference(run_id,digest,format,len(data))


async def put_json(run_id,token,value):
    from execution.tokens import canonical
    data=await resources().io.run(lambda item:canonical(item).encode(),value,owner=work_owner(),deadline=deadline(30))
    return await put_bytes(run_id,token,'json',data)


async def validate(conn,run_id,ref):
    value=ref.get(REF,{})
    if set(value)!={'run_id','sha256','format','bytes','version'} or value['run_id']!=str(run_id) or value['version']!=1:
        raise ValueError('Body dependency identity mismatch')
    row=await (await conn.execute('SELECT format,body,byte_length FROM agent.execution_bodies WHERE run_id=%s AND sha256=%s',(run_id,value['sha256']))).fetchone()
    if not row or row['format']!=value['format'] or row['byte_length']!=value['bytes'] or hashlib.sha256(row['format'].encode()+b'\0'+bytes(row['body'])).hexdigest()!=value['sha256']:
        raise ValueError('Body dependency missing or corrupted')
    return bytes(row['body'])


@retry_transaction
async def get_bytes(run_id,token,ref):
    async with transaction('AGENT_DATABASE_URL',readonly=True) as conn:
        await store.assert_active(conn,TaskRef(run_id,TaskKind.AGENT),token,lock=False)
        return await validate(conn,run_id,ref)


async def get_json(run_id,token,ref):
    data=await get_bytes(run_id,token,ref)
    return await resources().io.run(json.loads,data,owner=work_owner(),deadline=deadline(30))


async def resolve_evidence(context, evidence):
    result={}
    for key,item in evidence.items():
        result[key]=await resolve_one(context,item)
    return result


async def resolve_one(context,item):
    if isinstance(item,dict) and EVIDENCE_REF in item:
        return await get_json(context.run_id,context.lease_token,item[EVIDENCE_REF])
    if isinstance(item,dict) and '_tool_evidence' in item:
        link=item['_tool_evidence']
        key=link['body'][REF]['sha256']
        if key not in context.body_cache:
            context.body_cache[key]=await get_json(context.run_id,context.lease_token,link['body'])
        body=context.body_cache[key]
        resolved=body['evidence'][link['index']]
        if resolved['evidence_id']!=item['evidence_id']:raise ValueError('Tool evidence dependency mismatch')
        return resolved
    return item


async def compact_evidence(context,evidence):
    result={}
    for key,item in evidence.items():
        if EVIDENCE_REF in item or '_tool_evidence' in item:result[key]=item;continue
        result[key]={EVIDENCE_REF:await put_json(context.run_id,context.lease_token,item),
                     'evidence_id':key,'title':item.get('title',''),'source_locator':item.get('source_locator',{})}
    return result


async def pin(conn,run_id,ref,owner):
    await validate(conn,run_id,ref)
    await conn.execute('INSERT INTO agent.body_pins(run_id,sha256,owner) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                       (run_id,ref[REF]['sha256'],owner))
