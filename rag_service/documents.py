import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile

from contracts.errors import ApiError
from contracts.v1 import ErrorCode
from db.connection import connect


EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
}


def files_root() -> Path:
    root = Path(os.environ.get("RAG_FILES_DIR", ".data/rag")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def stage_file(file: UploadFile) -> tuple[Path, bytes, str]:
    media_type = EXTENSIONS.get(Path(file.filename or "").suffix.lower())
    if not media_type:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Unsupported file format")
    limit = int(os.environ.get("RAG_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    digest = hashlib.sha256()
    size = 0
    fd, name = tempfile.mkstemp(prefix="upload-", dir=files_root())
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ApiError(422, ErrorCode.INVALID_REQUEST, "File too large")
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise ApiError(422, ErrorCode.INVALID_REQUEST, "Empty file")
        return path, digest.digest(), media_type
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _validate_key(key: str | None) -> None:
    if key is not None and (not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid Idempotency-Key")


def _request_digest(file_digest: bytes, media_type: str, *metadata: str) -> bytes:
    framed = [file_digest, media_type.encode()]
    framed.extend(len(item.encode()).to_bytes(4, "big") + item.encode() for item in metadata)
    return hashlib.sha256(b"".join(framed)).digest()


def _lock_id(*parts: str) -> int:
    framed = b"".join(len(part.encode()).to_bytes(4, "big") + part.encode() for part in parts)
    return int.from_bytes(hashlib.sha256(framed).digest()[:8], "big", signed=True)


def create_document(file: UploadFile, title: str, visibility: str, team_ids: list[UUID], key: str | None, caller_key_id: str) -> dict:
    _validate_key(key)
    if visibility not in {"public", "restricted"} or not title.strip():
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid document metadata")
    staged, digest, media_type = stage_file(file)
    document_id, version_id = uuid4(), uuid4()
    target = files_root() / f"{version_id}{Path(file.filename or '').suffix.lower()}"
    normalized_teams = sorted({str(item) for item in team_ids})
    request_digest = _request_digest(digest, media_type, title.strip(), visibility, *normalized_teams)
    moved = False
    try:
        with connect("RAG_DATABASE_URL") as conn:
            if key:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(caller_key_id, "create", key),))
                existing = conn.execute("""SELECT u.request_digest,u.document_id,v.id,v.status FROM rag.upload_keys u
                    JOIN rag.document_versions v ON v.id=u.version_id
                    WHERE u.caller_key_id=%s AND u.scope='create' AND u.key=%s""", (caller_key_id, key)).fetchone()
                if existing:
                    if bytes(existing["request_digest"]) != request_digest:
                        raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key conflict")
                    return {"document_id": str(existing["document_id"]), "document_version_id": str(existing["id"]), "status": existing["status"]}
            for team_id in team_ids:
                if not conn.execute("SELECT 1 FROM identity.teams WHERE id=%s", (team_id,)).fetchone():
                    raise ApiError(422, ErrorCode.INVALID_REQUEST, "Unknown team")
            staged.replace(target)
            moved = True
            conn.execute("INSERT INTO rag.documents(id,title,visibility) VALUES (%s,%s,%s)", (document_id, title.strip(), visibility))
            conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,1,%s,%s,%s,'pending')", (version_id, document_id, digest, str(target), media_type))
            for team_id in set(team_ids):
                conn.execute("INSERT INTO rag.document_access(document_id,team_id) VALUES (%s,%s)", (document_id, team_id))
            if key:
                conn.execute("""INSERT INTO rag.upload_keys(caller_key_id,scope,document_id,key,request_digest,version_id)
                    VALUES (%s,'create',%s,%s,%s,%s)""", (caller_key_id, document_id, key, request_digest, version_id))
            conn.execute("INSERT INTO rag.processing_jobs(id,version_id,status) VALUES (%s,%s,'queued')", (uuid4(), version_id))
    except Exception:
        if moved:
            target.unlink(missing_ok=True)
        raise
    finally:
        staged.unlink(missing_ok=True)
    return {"document_id": str(document_id), "document_version_id": str(version_id), "status": "pending"}


def add_version(document_id: UUID, file: UploadFile, key: str | None, caller_key_id: str) -> dict:
    _validate_key(key)
    staged, digest, media_type = stage_file(file)
    version_id = uuid4()
    target = files_root() / f"{version_id}{Path(file.filename or '').suffix.lower()}"
    request_digest = _request_digest(digest, media_type, str(document_id))
    scope = "version:" + str(document_id)
    moved = False
    try:
        with connect("RAG_DATABASE_URL") as conn:
            doc = conn.execute("SELECT id,deleted_at FROM rag.documents WHERE id=%s FOR UPDATE", (document_id,)).fetchone()
            if not doc or doc["deleted_at"]:
                raise ApiError(404, ErrorCode.NOT_FOUND, "Document not found")
            if key:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_id(caller_key_id, scope, key),))
                existing = conn.execute("""SELECT u.request_digest,u.document_id,v.id,v.status FROM rag.upload_keys u
                    JOIN rag.document_versions v ON v.id=u.version_id
                    WHERE u.caller_key_id=%s AND u.scope=%s AND u.key=%s""", (caller_key_id, scope, key)).fetchone()
                if existing:
                    if existing["document_id"] != document_id or bytes(existing["request_digest"]) != request_digest:
                        raise ApiError(409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key conflict")
                    return {"document_id": str(document_id), "document_version_id": str(existing["id"]), "status": existing["status"]}
            pending = conn.execute("SELECT 1 FROM rag.document_versions WHERE document_id=%s AND status IN ('pending','processing')", (document_id,)).fetchone()
            if pending:
                raise ApiError(409, ErrorCode.VERSION_IN_PROGRESS, "Version in progress")
            same = conn.execute("SELECT id,status FROM rag.document_versions WHERE document_id=%s AND file_sha256=%s AND media_type=%s AND status IN ('active','superseded') ORDER BY version_number DESC LIMIT 1", (document_id, digest, media_type)).fetchone()
            if same:
                if key:
                    conn.execute("""INSERT INTO rag.upload_keys(caller_key_id,scope,document_id,key,request_digest,version_id)
                        VALUES (%s,%s,%s,%s,%s,%s)""", (caller_key_id, scope, document_id, key, request_digest, same["id"]))
                return {"document_id": str(document_id), "document_version_id": str(same["id"]), "status": same["status"]}
            number = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 AS n FROM rag.document_versions WHERE document_id=%s", (document_id,)).fetchone()["n"]
            staged.replace(target)
            moved = True
            conn.execute("INSERT INTO rag.document_versions(id,document_id,version_number,file_sha256,file_path,media_type,status) VALUES (%s,%s,%s,%s,%s,%s,'pending')", (version_id, document_id, number, digest, str(target), media_type))
            if key:
                conn.execute("""INSERT INTO rag.upload_keys(caller_key_id,scope,document_id,key,request_digest,version_id)
                    VALUES (%s,%s,%s,%s,%s,%s)""", (caller_key_id, scope, document_id, key, request_digest, version_id))
            conn.execute("INSERT INTO rag.processing_jobs(id,version_id,status) VALUES (%s,%s,'queued')", (uuid4(), version_id))
        return {"document_id": str(document_id), "document_version_id": str(version_id), "status": "pending"}
    except Exception:
        if moved:
            target.unlink(missing_ok=True)
        raise
    finally:
        staged.unlink(missing_ok=True)


def version_status(document_id: UUID, version_id: UUID) -> dict:
    with connect("RAG_DATABASE_URL") as conn:
        row = conn.execute("""SELECT v.id,v.status,v.error_code,v.warnings,j.attempts,j.error_code AS job_error
            FROM rag.document_versions v JOIN rag.documents d ON d.id=v.document_id
            JOIN rag.processing_jobs j ON j.version_id=v.id
            WHERE d.id=%s AND v.id=%s""", (document_id, version_id)).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Version not found")
    return {"document_id": str(document_id), "document_version_id": str(version_id), "status": row["status"], "attempts": row["attempts"], "error_code": row["error_code"] or row["job_error"], "warnings": row["warnings"]}


def set_access(document_id: UUID, visibility: str, team_ids: list[UUID]) -> dict:
    if visibility not in {"public", "restricted"}:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Invalid visibility")
    with connect("RAG_DATABASE_URL") as conn:
        doc = conn.execute("SELECT id FROM rag.documents WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (document_id,)).fetchone()
        if not doc:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Document not found")
        for team_id in team_ids:
            if not conn.execute("SELECT 1 FROM identity.teams WHERE id=%s", (team_id,)).fetchone():
                raise ApiError(422, ErrorCode.INVALID_REQUEST, "Unknown team")
        conn.execute("UPDATE rag.documents SET visibility=%s WHERE id=%s", (visibility, document_id))
        conn.execute("DELETE FROM rag.document_access WHERE document_id=%s", (document_id,))
        for team_id in set(team_ids):
            conn.execute("INSERT INTO rag.document_access(document_id,team_id) VALUES (%s,%s)", (document_id, team_id))
    return {"document_id": str(document_id), "visibility": visibility, "team_ids": [str(x) for x in team_ids]}


def soft_delete(document_id: UUID) -> dict:
    with connect("RAG_DATABASE_URL") as conn:
        row = conn.execute("UPDATE rag.documents SET deleted_at=COALESCE(deleted_at,now()) WHERE id=%s RETURNING id,deleted_at", (document_id,)).fetchone()
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Document not found")
    return {"document_id": str(document_id), "deleted_at": row["deleted_at"]}
