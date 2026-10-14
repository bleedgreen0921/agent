"""Document state transactions; preparation and model calls stay outside."""
from uuid import uuid4
from psycopg.types.json import Jsonb
from execution import store
from execution.persistence import transaction
from execution.contracts import TaskRef, TaskKind
from rag_service.computation import vector_literal
from execution.retry import retry_transaction

LEASE_SECONDS=60


def task_ref(job): return TaskRef(job['id'],TaskKind.DOCUMENT)


@retry_transaction(profile='control')
async def claim(worker_id=None):
    async with transaction('RAG_DATABASE_URL',profile='control') as conn:
        row=await store.claim(conn,TaskKind.DOCUMENT,lease_seconds=LEASE_SECONDS,worker_id=worker_id)
        if not row:return None
        business=await (await conn.execute('SELECT version_id FROM rag.processing_jobs WHERE id=%s',(row['id'],))).fetchone()
        # This transaction only locks task state; version publication is separate.
        await store.assert_active(conn,TaskRef(row['id'],TaskKind.DOCUMENT),row['lease_token'])
        return {'id':row['id'],'version_id':business['version_id'],'token':row['lease_token'],
                'attempts':row['attempts'],'deadline_at':row['execution_deadline_at'],'remaining_seconds':float(row['remaining_seconds'])}


@retry_transaction
async def inputs(job):
    async with transaction('RAG_DATABASE_URL',readonly=True) as conn:
        row=await (await conn.execute('''SELECT v.file_path,v.media_type,v.document_id,d.title FROM rag.document_versions v
            JOIN rag.documents d ON d.id=v.document_id WHERE v.id=%s''',(job['version_id'],))).fetchone()
        revisions=await (await conn.execute("SELECT id,model,dimensions FROM rag.index_revisions WHERE state IN ('active','building') ORDER BY state")).fetchall()
    return row,revisions


@retry_transaction(profile='bulk')
async def publish(job,row,drafts,warnings,prepared):
    from rag_service.models import ModelFailure
    async with transaction('RAG_DATABASE_URL',profile='bulk') as conn:
        await conn.execute('SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE')
        current=await (await conn.execute("SELECT id FROM rag.index_revisions WHERE state IN ('active','building')")).fetchall()
        if {i['id'] for i in current}!={r['id'] for r,_ in prepared}:raise ModelFailure('INDEX_CONFIGURATION_CHANGED')
        doc=await (await conn.execute('SELECT deleted_at FROM rag.documents WHERE id=%s FOR UPDATE',(row['document_id'],))).fetchone()
        if not doc or doc['deleted_at']:raise ValueError('Document has been deleted')
        await conn.execute('DELETE FROM rag.chunks WHERE version_id=%s',(job['version_id'],))
        for ordinal,draft in enumerate(drafts,1):
            chunk_id=uuid4()
            await conn.execute('''INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,heading_path,source_locator,table_html,search_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(chunk_id,row['document_id'],job['version_id'],ordinal,draft['content'],Jsonb(draft['heading_path']),Jsonb(draft['locator']),draft['table_html'],draft['search_text']))
            for revision,vectors in prepared:
                await conn.execute('INSERT INTO rag.embeddings(chunk_id,revision_id,embedding) VALUES (%s,%s,%s::vector)',(chunk_id,revision['id'],vector_literal(vectors[ordinal-1])))
        await store.assert_active(conn,task_ref(job),job['token'])
        await conn.execute("UPDATE rag.document_versions SET status='superseded' WHERE document_id=%s AND status='active'",(row['document_id'],))
        await conn.execute("UPDATE rag.document_versions SET status='active',error_code=NULL,warnings=%s,activated_at=clock_timestamp() WHERE id=%s",(Jsonb(warnings),job['version_id']))
        await conn.execute('UPDATE rag.documents SET active_version_id=%s WHERE id=%s',(job['version_id'],row['document_id']))
        await store.complete(conn,task_ref(job),job['token'])


@retry_transaction(profile='control')
async def fail(job,code,retryable,*,worker_exit=False):
    async with transaction('RAG_DATABASE_URL',profile='control') as conn:
        # No document/index locks here: control never inverts the bulk lock order.
        result=await store.settle_failure(conn,task_ref(job),job['token'],code,retryable=retryable,worker_exit=worker_exit)
    return result.accepted
