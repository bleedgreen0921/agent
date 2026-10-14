import hashlib
from uuid import UUID, uuid4
from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from execution.persistence import transaction as connect
from execution import store
from execution.contracts import TaskRef, TaskKind
from execution.retry import retry_transaction

def _validate_key(key: str | None) -> None:
    if key is not None and (not 1 <= len(key) <= 128 or any((ord(c) < 33 or ord(c) > 126 for c in key))):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Invalid Idempotency-Key')

def _request_digest(file_digest: bytes, media_type: str, *metadata: str) -> bytes:
    framed = [file_digest, media_type.encode()]
    framed.extend((len(item.encode()).to_bytes(4, 'big') + item.encode() for item in metadata))
    return hashlib.sha256(b''.join(framed)).digest()

def _lock_id(*parts: str) -> int:
    framed = b''.join((len(part.encode()).to_bytes(4, 'big') + part.encode() for part in parts))
    return int.from_bytes(hashlib.sha256(framed).digest()[:8], 'big', signed=True)

@retry_transaction
async def create_document(prepared):
    document_id, version_id = (prepared['document_id'], prepared['version_id'])
    target, digest, media_type = (prepared['file_path'], prepared['digest'], prepared['media_type'])
    key = prepared['key']
    _validate_key(key)
    title = prepared['title']
    request_digest = _request_digest(digest, media_type, title.strip())
    async with connect('RAG_DATABASE_URL') as conn:
        from execution.artifacts import reference
        await reference(conn,prepared)
        if key:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (_lock_id('create', key),))
            existing = await (await conn.execute("SELECT u.request_digest,u.document_id,v.id,v.status FROM rag.upload_keys u\n                    JOIN rag.document_versions v ON v.id=u.version_id\n                    WHERE u.scope='create' AND u.key=%s", (key,))).fetchone()
            if existing:
                if bytes(existing['request_digest']) != request_digest:
                    raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, 'Idempotency key conflict')
                return {'document_id': str(existing['document_id']), 'document_version_id': str(existing['id']), 'status': existing['status']}
        await conn.execute('INSERT INTO rag.documents(id,title) VALUES (%s,%s)', (document_id, title.strip()))
        await conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,1,%s,%s,%s,'pending')", (version_id, document_id, digest, str(target), media_type))
        if key:
            await conn.execute("INSERT INTO rag.upload_keys(scope,document_id,key,request_digest,version_id)\n                    VALUES ('create',%s,%s,%s,%s)", (document_id, key, request_digest, version_id))
        job_id = uuid4()
        await store.create(conn, TaskRef(job_id, TaskKind.DOCUMENT))
        await conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (job_id, version_id))
    return {'document_id': str(document_id), 'document_version_id': str(version_id), 'status': 'pending'}

@retry_transaction
async def add_version(prepared):
    document_id, version_id = (prepared['document_id'], prepared['version_id'])
    target, digest, media_type = (prepared['file_path'], prepared['digest'], prepared['media_type'])
    key = prepared['key']
    _validate_key(key)
    request_digest = _request_digest(digest, media_type, str(document_id))
    scope = 'version:' + str(document_id)
    async with connect('RAG_DATABASE_URL') as conn:
        from execution.artifacts import reference
        await reference(conn,prepared)
        doc = await (await conn.execute('SELECT id,deleted_at FROM rag.documents WHERE id=%s FOR UPDATE', (document_id,))).fetchone()
        if not doc or doc['deleted_at']:
            raise ApiError(404, ErrorCode.NOT_FOUND, 'Document not found')
        if key:
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (_lock_id(scope, key),))
            existing = await (await conn.execute('SELECT u.request_digest,u.document_id,v.id,v.status FROM rag.upload_keys u\n                    JOIN rag.document_versions v ON v.id=u.version_id\n                    WHERE u.scope=%s AND u.key=%s', (scope, key))).fetchone()
            if existing:
                if existing['document_id'] != document_id or bytes(existing['request_digest']) != request_digest:
                    raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, 'Idempotency key conflict')
                return {'document_id': str(document_id), 'document_version_id': str(existing['id']), 'status': existing['status']}
        # Reconcile terminal processing metadata while holding only the document lock.
        # Control settlement never acquires this lock after locking a task.
        await conn.execute("""UPDATE rag.document_versions v SET status='failed',error_code=t.error_code
            FROM rag.processing_jobs j JOIN execution.tasks t ON t.id=j.id AND t.kind='rag_document'
            WHERE j.version_id=v.id AND v.document_id=%s AND v.status IN ('pending','processing')
              AND t.status IN ('failed','cancelled')""",(document_id,))
        pending = await (await conn.execute("SELECT 1 FROM rag.document_versions v JOIN rag.processing_jobs j ON j.version_id=v.id JOIN execution.tasks t ON t.id=j.id AND t.kind='rag_document' WHERE v.document_id=%s AND t.status IN ('queued','running','stopping')", (document_id,))).fetchone()
        if pending:
            raise ApiError(409, ErrorCode.VERSION_IN_PROGRESS, 'Version in progress')
        same = await (await conn.execute("SELECT id,status FROM rag.document_versions WHERE document_id=%s AND file_sha256=%s AND media_type=%s AND status IN ('active','superseded') ORDER BY version_number DESC LIMIT 1", (document_id, digest, media_type))).fetchone()
        if same:
            if key:
                await conn.execute('INSERT INTO rag.upload_keys(scope,document_id,key,request_digest,version_id)\n                        VALUES (%s,%s,%s,%s,%s)', (scope, document_id, key, request_digest, same['id']))
            return {'document_id': str(document_id), 'document_version_id': str(same['id']), 'status': same['status']}
        number = (await (await conn.execute('SELECT COALESCE(MAX(version_number),0)+1 AS n FROM rag.document_versions WHERE document_id=%s', (document_id,))).fetchone())['n']
        await conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,%s,%s,%s,%s,'pending')", (version_id, document_id, number, digest, str(target), media_type))
        if key:
            await conn.execute('INSERT INTO rag.upload_keys(scope,document_id,key,request_digest,version_id)\n                    VALUES (%s,%s,%s,%s,%s)', (scope, document_id, key, request_digest, version_id))
        job_id = uuid4()
        await store.create(conn, TaskRef(job_id, TaskKind.DOCUMENT))
        await conn.execute('INSERT INTO rag.processing_jobs(id,version_id) VALUES (%s,%s)', (job_id, version_id))
    return {'document_id': str(document_id), 'document_version_id': str(version_id), 'status': 'pending'}

async def version_status(document_id: UUID, version_id: UUID) -> dict:
    async with connect('RAG_DATABASE_URL') as conn:
        row = await (await conn.execute("SELECT v.id,j.id AS job_id,CASE WHEN v.status IN ('active','superseded') THEN v.status WHEN j.status='running' THEN 'processing' WHEN j.status='failed' THEN 'failed' ELSE 'pending' END AS status,v.error_code,v.warnings,j.attempts,j.error_code AS job_error\n            FROM rag.document_versions v JOIN rag.documents d ON d.id=v.document_id\n            JOIN rag.processing_job_state j ON j.version_id=v.id\n            WHERE d.id=%s AND v.id=%s", (document_id, version_id))).fetchone()
        from execution.progress import ProgressStore
        progress = await ProgressStore().snapshot(conn, row['job_id']) if row else None
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, 'Version not found')
    return {'document_id': str(document_id), 'document_version_id': str(version_id), 'status': row['status'], 'attempts': row['attempts'], 'error_code': row['error_code'] or row['job_error'], 'warnings': row['warnings'], 'progress': progress}

@retry_transaction
async def soft_delete(document_id: UUID) -> dict:
    async with connect('RAG_DATABASE_URL') as conn:
        row = await (await conn.execute('SELECT id,deleted_at FROM rag.documents WHERE id=%s FOR UPDATE', (document_id,))).fetchone()
        if row and row['deleted_at'] is None:
            # UPDATE expressions may be evaluated before a row-lock wait.
            # Acquire the lock first, then timestamp the actual state change.
            row = await (await conn.execute('UPDATE rag.documents SET deleted_at=clock_timestamp() WHERE id=%s RETURNING id,deleted_at', (document_id,))).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, 'Document not found')
    return {'document_id': str(document_id), 'deleted_at': row['deleted_at']}

async def referenced(path):
    async with connect('RAG_DATABASE_URL', readonly=True) as conn:
        return bool(await (await conn.execute('SELECT 1 FROM rag.document_versions WHERE file_path=%s', (path,))).fetchone())
