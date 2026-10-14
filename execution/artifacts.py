"""Registered upload ownership; a reference and its state commit together."""
from uuid import uuid4
from execution.persistence import transaction
from execution.retry import retry_transaction


@retry_transaction
async def register(path):
    identity,writer=uuid4(),uuid4()
    async with transaction() as conn:
        await conn.execute("INSERT INTO execution.artifacts(namespace,id,path,state,writer_id,writer_until) VALUES ('rag',%s,%s,'writing',%s,clock_timestamp()+interval '15 minutes')",(identity,str(path),writer))
    return identity


@retry_transaction
async def prepared(identity,target,digest):
    async with transaction() as conn:
        row=await (await conn.execute("UPDATE execution.artifacts SET state='prepared',target_path=%s,sha256=%s,updated_at=clock_timestamp() WHERE namespace='rag' AND id=%s AND state='writing' RETURNING id",(str(target),digest.hex(),identity))).fetchone()
        if not row:raise ValueError('Upload artifact no longer belongs to its writer')


async def reference(conn,prepared):
    identity=prepared.get('artifact_id')
    if identity is None:return
    row=await (await conn.execute("SELECT * FROM execution.artifacts WHERE namespace='rag' AND id=%s FOR UPDATE",(identity,))).fetchone()
    if not row or row['state'] not in ('prepared','referenced','unknown') or row['target_path']!=prepared['file_path'] or row['sha256']!=prepared['digest'].hex():
        raise ValueError('Upload artifact was quarantined or changed')
    await conn.execute("UPDATE execution.artifacts SET state='referenced',updated_at=clock_timestamp() WHERE namespace='rag' AND id=%s",(identity,))


@retry_transaction
async def unknown(identity):
    async with transaction() as conn:
        await conn.execute("UPDATE execution.artifacts SET state='unknown',updated_at=clock_timestamp() WHERE namespace='rag' AND id=%s AND state='prepared'",(identity,))
