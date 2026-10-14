"""Shadow embedding revision build and atomic global switch."""
import argparse
from uuid import UUID, uuid4
from execution.persistence import transaction as connect
from rag_service.chunking import ChunkDraft, embedding_input
from rag_service.computation import vector_literal
from execution.retry import retry_transaction

@retry_transaction
async def start(model: str, dimensions: int) -> UUID:
    if not model or not 1 <= dimensions <= 4096:
        raise ValueError('invalid revision configuration')
    revision_id = uuid4()
    async with connect('RAG_DATABASE_URL') as conn:
        await conn.execute('SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE')
        if await (await conn.execute("SELECT 1 FROM rag.index_revisions WHERE state='building'")).fetchone():
            raise ValueError('shadow revision already building')
        await conn.execute("INSERT INTO rag.index_revisions(id,model,dimensions,document_template,query_template,state)\n            VALUES (%s,%s,%s,'title-content-v1','qwen-retrieval-v2','building')", (revision_id, model, dimensions))
    return revision_id

@retry_transaction
async def switch(revision_id: UUID) -> None:
    async with connect('RAG_DATABASE_URL') as conn:
        state = await (await conn.execute('SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE')).fetchone()
        if not await (await conn.execute("SELECT 1 FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,))).fetchone():
            raise ValueError('revision is not building')
        missing = (await (await conn.execute('SELECT count(*) AS n FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id\n            WHERE d.deleted_at IS NULL AND d.active_version_id=c.version_id\n              AND NOT EXISTS (SELECT 1 FROM rag.embeddings e WHERE e.chunk_id=c.id AND e.revision_id=%s)', (revision_id,))).fetchone())['n']
        if missing:
            raise ValueError(f'shadow revision missing {missing} active chunks')
        await conn.execute("UPDATE rag.index_revisions SET state='superseded' WHERE id=%s", (state['active_revision_id'],))
        await conn.execute("UPDATE rag.index_revisions SET state='active' WHERE id=%s", (revision_id,))
        await conn.execute('UPDATE rag.index_state SET active_revision_id=%s WHERE singleton=true', (revision_id,))

async def next_batch(revision_id, batch_size):
    async with connect('RAG_DATABASE_URL') as conn:
        revision = await (await conn.execute("SELECT id,model,dimensions FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,))).fetchone()
        if not revision:
            raise ValueError('revision is not building')
        rows = await (await conn.execute('SELECT c.id,c.document_id,c.version_id,c.content,c.heading_path,d.title\n                FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id\n                WHERE d.deleted_at IS NULL AND d.active_version_id=c.version_id\n                  AND NOT EXISTS (SELECT 1 FROM rag.embeddings e WHERE e.chunk_id=c.id AND e.revision_id=%s)\n                ORDER BY c.id LIMIT %s', (revision_id, batch_size))).fetchall()
    return (revision, rows)

@retry_transaction
async def publish_batch(revision_id, rows, vectors):
    completed = 0
    async with connect('RAG_DATABASE_URL') as conn:
        await conn.execute('SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE')
        if not await (await conn.execute("SELECT 1 FROM rag.index_revisions WHERE id=%s AND state='building'", (revision_id,))).fetchone():
            raise ValueError('revision changed during build')
        for row, vector in zip(rows, vectors):
            doc = await (await conn.execute('SELECT active_version_id,deleted_at FROM rag.documents WHERE id=%s FOR UPDATE', (row['document_id'],))).fetchone()
            if doc['deleted_at'] or doc['active_version_id'] != row['version_id']:
                continue
            await conn.execute('INSERT INTO rag.embeddings(chunk_id,revision_id,embedding)\n                    VALUES (%s,%s,%s::vector) ON CONFLICT DO NOTHING', (row['id'], revision_id, vector_literal(vector)))
            completed += 1
    return completed
