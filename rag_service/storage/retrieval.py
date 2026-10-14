from uuid import UUID, uuid4
from dataclasses import dataclass
import psycopg
from psycopg.types.json import Jsonb
from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from execution.persistence import transaction as connect
from rag_service.computation import vector_literal
from execution.retry import retry_transaction
VISIBLE = 'd.deleted_at IS NULL AND d.active_version_id=c.version_id'
VISIBLE_HISTORY = 'd.deleted_at IS NULL'

async def _revision(conn) -> dict:
    row = await (await conn.execute("SELECT r.id,r.model,r.dimensions FROM rag.index_state s\n        JOIN rag.index_revisions r ON r.id=s.active_revision_id WHERE s.singleton=true AND r.state='active'")).fetchone()
    if not row:
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, 'Index unavailable')
    return row

async def _dense(conn, revision: dict, vector: list[float]) -> list[dict]:
    dimension = int(revision['dimensions'])
    if not 1 <= dimension <= 4096:
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, 'Index configuration invalid')
    query = f'SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title,\n        e.embedding::vector({dimension}) <=> %s::vector({dimension}) AS distance\n        FROM rag.embeddings e JOIN rag.chunks c ON c.id=e.chunk_id\n        JOIN rag.documents d ON d.id=c.document_id\n        WHERE e.revision_id=%s AND {VISIBLE}\n        ORDER BY e.embedding::vector({dimension}) <=> %s::vector({dimension}), c.id LIMIT 50'
    literal = vector_literal(vector)
    return await (await conn.execute(query, (literal, revision['id'], literal))).fetchall()

async def _sparse(conn, query: str) -> list[dict]:
    terms = query
    if not terms:
        return []
    return await (await conn.execute(f"SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title,\n        ts_rank_cd(c.search_vector,plainto_tsquery('simple',%s)) AS score\n        FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id\n        WHERE {VISIBLE} AND c.search_vector @@ plainto_tsquery('simple',%s)\n        ORDER BY score DESC,c.id LIMIT 50", (terms, terms))).fetchall()

@retry_transaction
async def _audit(request_id: str, run_id: str, tool_call_id: str, revision_id: UUID | None, status: str, degradations: list[str], *, operation: str, evidence_id: UUID | None=None, retrieval_id: str | None=None, query_sha256: str | None=None, rewritten_query_sha256: str | None=None, selected_evidence_ids: list[str] | None=None, error_code: str | None=None, candidate_trace: dict | None=None) -> None:
    async with connect('RAG_DATABASE_URL') as conn:
        await conn.execute('INSERT INTO rag.retrieval_audit(\n            id,request_id,run_id,tool_call_id,revision_id,status,degradations,evidence_id,\n            operation,retrieval_id,query_sha256,rewritten_query_sha256,selected_evidence_ids,error_code,candidate_trace)\n            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (uuid4(), request_id, run_id, tool_call_id, revision_id, status, Jsonb(degradations), evidence_id, operation, retrieval_id, query_sha256, rewritten_query_sha256, Jsonb(selected_evidence_ids) if selected_evidence_ids is not None else None, error_code, Jsonb(candidate_trace) if candidate_trace is not None else None))

def _evidence(row: dict, rank: int | None=None) -> dict:
    result = {'evidence_id': 'ev_' + str(row['id']), 'document_id': 'doc_' + str(row['document_id']), 'document_version_id': 'ver_' + str(row['version_id']), 'title': row['title'], 'content': row['content'], 'source_locator': row['source_locator']}
    if rank is not None:
        result['rank'] = rank
    return result

async def read_evidence(evidence_id: str, run_id: str, tool_call_id: str) -> dict:
    request_id = 'rag_req_' + uuid4().hex
    try:
        chunk_id = UUID(evidence_id.removeprefix('ev_')) if evidence_id.startswith('ev_') else None
    except ValueError:
        chunk_id = None
    async with connect('RAG_DATABASE_URL') as conn:
        state = await (await conn.execute('SELECT active_revision_id FROM rag.index_state WHERE singleton=true')).fetchone()
        row = await (await conn.execute(f'SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title\n            FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id\n            WHERE c.id=%s AND {VISIBLE_HISTORY}', (chunk_id,))).fetchone() if chunk_id else None
    await _audit(request_id, run_id, tool_call_id, state['active_revision_id'] if state else None, 'read_ok' if row else 'not_found', [], operation='read', evidence_id=chunk_id, selected_evidence_ids=['ev_' + str(chunk_id)] if row else [], error_code=None if row else 'EVIDENCE_NOT_FOUND')
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, 'Evidence not found')
    return _evidence(row)

@retry_transaction
async def revision():
    async with connect('RAG_DATABASE_URL', readonly=True) as conn:
        return await _revision(conn)

@dataclass(frozen=True)
class RecallSnapshot:
    revision: dict
    dense: list[dict] | None
    sparse: list[dict] | None


@retry_transaction
async def recall(prepared_revision: dict, vector: list[float] | None, terms: str) -> RecallSnapshot:
    """Materialize both branches from one read-only database snapshot.

    Provider/CPU preparation happens before this short transaction. A revision
    switch with the same model and dimensions can reuse the query embedding;
    a changed embedding configuration makes only the dense branch unavailable.
    """
    async with connect('RAG_DATABASE_URL', readonly=True, isolation='repeatable read') as conn:
        current = await _revision(conn)
        dense = sparse = None
        compatible = all(current[key] == prepared_revision[key] for key in ('model', 'dimensions'))
        if vector is not None and compatible:
            try:
                # Recover a branch-local SQL timeout without aborting the other
                # query or changing the enclosing REPEATABLE READ snapshot.
                async with conn.transaction():
                    from execution.timing import span
                    with span('dense_sql','sql'):dense = await _dense(conn, current, vector)
            except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
                raise
            except psycopg.OperationalError:
                if conn.closed:
                    raise
        try:
            async with conn.transaction():
                from execution.timing import span
                with span('fts_sql','sql'):sparse = await _sparse(conn, terms)
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
            raise
        except psycopg.OperationalError:
            if conn.closed:
                raise
        return RecallSnapshot(current, dense, sparse)
