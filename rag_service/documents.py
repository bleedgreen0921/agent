import hashlib
import os
import threading
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import UploadFile
from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from execution.persistence import resources, deadline, PersistenceOutcomeUnknown
from execution.scope import work_owner
from execution.stores import stores
EXTENSIONS = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.md': 'text/markdown', '.txt': 'text/plain'}

def files_root() -> Path:
    root = Path(os.environ.get('RAG_FILES_DIR', '.data/personal-rag')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root

def _write_file(file, path, limit, stopped):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    lock=path.with_name(path.name+'.lock').open('a+b')
    fcntl.flock(lock,fcntl.LOCK_EX)
    try:
        with path.open('xb') as output:
            while (chunk := file.read(1024 * 1024)):
                if stopped.is_set():
                    raise InterruptedError('Upload stopped')
                size += len(chunk)
                if size > limit:
                    raise ApiError(422, ErrorCode.INVALID_REQUEST, 'File too large')
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Empty file')
        return digest.digest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        if stopped.is_set():
            path.unlink(missing_ok=True)
        lock.close()

async def stage_file(file):
    media_type = EXTENSIONS.get(Path(file.filename or '').suffix.lower())
    if not media_type:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Unsupported file format')
    path = Path(os.environ.get('RAG_FILES_DIR', '.data/personal-rag')).absolute() / ('upload-' + uuid4().hex)
    stopped = threading.Event()
    from execution.artifacts import register
    identity=await register(path)
    try:
        digest = await resources().io.run(_write_file, file.file, path, int(os.environ.get('RAG_MAX_UPLOAD_BYTES', str(25 * 1024 * 1024))), stopped, owner=work_owner(), deadline=deadline(120))
        return (path, digest, media_type,identity)
    except BaseException:
        stopped.set()
        if resources().accepting:
            await resources().cleanup(resources().io.run(path.unlink, missing_ok=True, deadline=deadline(5)))
        raise

def _validate_key(key: str | None) -> None:
    if key is not None and (not 1 <= len(key) <= 128 or any((ord(c) < 33 or ord(c) > 126 for c in key))):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Invalid Idempotency-Key')

async def _upload(file, key, *, title=None, document_id=None):
    _validate_key(key)
    if title is not None and (not title.strip()):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, 'Invalid document metadata')
    from execution.admission import AdmissionStore
    from execution.upload_admission import upload_admitted
    from contextlib import nullcontext
    async with nullcontext() if upload_admitted.get() else AdmissionStore().request('upload', deadline(120)):
        staged, digest, media_type,artifact_id = await stage_file(file)
    version_id = uuid4()
    target = staged.parent / (str(version_id) + Path(file.filename or '').suffix.lower())
    uncertain = False
    prepared = {'document_id': document_id or uuid4(), 'version_id': version_id, 'file_path': str(target), 'digest': digest, 'media_type': media_type, 'title': title, 'key': key,'artifact_id':artifact_id}
    try:
        from execution.artifacts import prepared as prepare_artifact
        await prepare_artifact(artifact_id,target,digest)
        await resources().io.run(staged.replace, target, owner=work_owner(), deadline=deadline(30))
        operation = stores()[3].create_document if document_id is None else stores()[3].add_version
        return await operation(prepared)
    except PersistenceOutcomeUnknown:
        uncertain = True
        from execution.artifacts import unknown
        try:await unknown(artifact_id)
        except BaseException:pass
        raise
    finally:
        if not uncertain and resources().accepting:

            async def cleanup():
                if not await stores()[3].referenced(str(target)):
                    await resources().io.run(target.unlink, missing_ok=True, deadline=deadline(5))
                await resources().io.run(staged.unlink, missing_ok=True, deadline=deadline(5))
            await resources().cleanup(cleanup())

async def create_document(file, title, key):
    return await _upload(file, key, title=title)

async def add_version(document_id, file, key):
    return await _upload(file, key, document_id=document_id)

async def version_status(document_id, version_id):
    return await stores()[3].version_status(document_id, version_id)

async def soft_delete(document_id):
    return await stores()[3].soft_delete(document_id)
