"""PostgreSQL leased document worker; run with python -m rag_service.worker."""

import logging
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.connection import connect
from rag_service.chunking import embedding_input, make_chunks
from rag_service.models import ModelFailure, embed
from rag_service.parse import ParseFailure, parse_file


log = logging.getLogger(__name__)
LEASE_SECONDS = 60


def claim() -> dict | None:
    with connect("RAG_DATABASE_URL") as conn:
        exhausted = conn.execute("""SELECT id,version_id FROM rag.processing_jobs
            WHERE status='running' AND leased_until<now() AND attempts>=3
            FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if exhausted:
            conn.execute("UPDATE rag.processing_jobs SET status='failed',error_code='WORKER_INTERRUPTED',lease_token=NULL,leased_until=NULL,updated_at=now() WHERE id=%s", (exhausted["id"],))
            conn.execute("UPDATE rag.document_versions SET status='failed',error_code='WORKER_INTERRUPTED' WHERE id=%s", (exhausted["version_id"],))
        row = conn.execute("""SELECT j.id,j.version_id,j.attempts FROM rag.processing_jobs j
            WHERE (j.status IN ('queued','retry') AND j.available_at<=now())
               OR (j.status='running' AND j.leased_until<now() AND j.attempts<3)
            ORDER BY j.available_at, j.id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:
            return None
        token = uuid4()
        conn.execute("""UPDATE rag.processing_jobs SET status='running',attempts=attempts+1,
            lease_token=%s,leased_until=now()+interval '60 seconds',heartbeat_at=now(),updated_at=now()
            WHERE id=%s""", (token, row["id"]))
        conn.execute("UPDATE rag.document_versions SET status='processing' WHERE id=%s", (row["version_id"],))
        return {"id": row["id"], "version_id": row["version_id"], "token": token, "attempts": row["attempts"] + 1}


def heartbeat(job: dict, stop: threading.Event) -> None:
    while not stop.wait(10):
        try:
            with connect("RAG_DATABASE_URL") as conn:
                row = conn.execute("""UPDATE rag.processing_jobs SET leased_until=now()+interval '60 seconds',heartbeat_at=now()
                    WHERE id=%s AND lease_token=%s AND status='running' RETURNING id""", (job["id"], job["token"])).fetchone()
                if not row:
                    stop.set()
                    return
        except Exception:
            log.exception("Document worker heartbeat failed")


def process(job: dict) -> None:
    with connect("RAG_DATABASE_URL") as conn:
        row = conn.execute("""SELECT v.file_path,v.media_type,v.document_id,d.title FROM rag.document_versions v
            JOIN rag.documents d ON d.id=v.document_id WHERE v.id=%s""", (job["version_id"],)).fetchone()
        revisions = conn.execute("SELECT id,model,dimensions FROM rag.index_revisions WHERE state IN ('active','building') ORDER BY state").fetchall()
    if not row or not revisions:
        raise ParseFailure("INDEX_CONFIGURATION_INVALID")
    blocks, warnings = parse_file(Path(row["file_path"]), row["media_type"])
    drafts = make_chunks(blocks, row["title"])
    if len(drafts) > int(os.environ.get("RAG_MAX_CHUNKS", "2000")):
        raise ParseFailure("TOO_MANY_CHUNKS")
    prepared = []
    for revision in revisions:
        vectors = []
        for start in range(0, len(drafts), 16):
            texts = [embedding_input(row["title"], chunk) for chunk in drafts[start:start + 16]]
            vectors.extend(embed(texts, revision["model"], revision["dimensions"]))
        prepared.append((revision, vectors))
    with connect("RAG_DATABASE_URL") as conn:
        # The same lock is used by shadow switching and document publication.
        conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE")
        current = conn.execute("SELECT id FROM rag.index_revisions WHERE state IN ('active','building')").fetchall()
        if {item["id"] for item in current} != {revision["id"] for revision, _ in prepared}:
            raise ModelFailure("INDEX_CONFIGURATION_CHANGED")
        active_job = conn.execute("""SELECT id FROM rag.processing_jobs WHERE id=%s AND lease_token=%s
            AND status='running' AND leased_until>now() FOR UPDATE""", (job["id"], job["token"])).fetchone()
        if not active_job:
            raise ModelFailure("LEASE_LOST")
        conn.execute("SELECT id FROM rag.documents WHERE id=%s FOR UPDATE", (row["document_id"],))
        conn.execute("DELETE FROM rag.chunks WHERE version_id=%s", (job["version_id"],))
        for ordinal, draft in enumerate(drafts, 1):
            chunk_id = uuid4()
            conn.execute("""INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,heading_path,source_locator,table_html,search_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (chunk_id, row["document_id"], job["version_id"], ordinal, draft.content, Jsonb(list(draft.heading_path)), Jsonb(draft.locator), draft.table_html, sparse_terms(draft.content)))
            for revision, vectors in prepared:
                conn.execute("INSERT INTO rag.embeddings(chunk_id,revision_id,embedding) VALUES (%s,%s,%s::vector)", (chunk_id, revision["id"], vector_literal(vectors[ordinal - 1])))
        conn.execute("""UPDATE rag.document_versions SET status='superseded' WHERE document_id=%s AND status='active'""", (row["document_id"],))
        conn.execute("""UPDATE rag.document_versions SET status='active',error_code=NULL,warnings=%s,activated_at=now()
            WHERE id=%s""", (Jsonb(warnings), job["version_id"]))
        conn.execute("UPDATE rag.documents SET active_version_id=%s WHERE id=%s", (job["version_id"], row["document_id"]))
        conn.execute("""UPDATE rag.processing_jobs SET status='done',lease_token=NULL,leased_until=NULL,
            error_code=NULL,updated_at=now() WHERE id=%s""", (job["id"],))


def sparse_terms(content: str) -> str:
    import jieba
    import re

    words = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+", content)
    return " ".join(part.lower() for word in words for part in jieba.cut(word) if part.strip())


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


def fail(job: dict, code: str, retryable: bool) -> None:
    with connect("RAG_DATABASE_URL") as conn:
        row = conn.execute("""SELECT id FROM rag.processing_jobs WHERE id=%s AND lease_token=%s
            AND status='running' FOR UPDATE""", (job["id"], job["token"])).fetchone()
        if not row:
            return
        retry = retryable and job["attempts"] < 3
        conn.execute("""UPDATE rag.processing_jobs SET status=%s,error_code=%s,lease_token=NULL,leased_until=NULL,
            available_at=CASE WHEN %s THEN now()+interval '10 seconds' ELSE available_at END,updated_at=now()
            WHERE id=%s""", ("retry" if retry else "failed", code, retry, job["id"]))
        conn.execute("UPDATE rag.document_versions SET status=%s,error_code=%s WHERE id=%s", ("pending" if retry else "failed", code, job["version_id"]))


def run_once() -> bool:
    job = claim()
    if not job:
        return False
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(job, stop), daemon=True)
    thread.start()
    try:
        process(job)
    except ParseFailure as exc:
        fail(job, str(exc), False)
    except ModelFailure as exc:
        fail(job, exc.code, exc.retryable)
    except Exception:
        log.exception("Document processing failed")
        fail(job, "PROCESSING_FAILED", False)
    finally:
        stop.set()
        thread.join(timeout=2)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        if not run_once():
            time.sleep(1)


if __name__ == "__main__":
    main()
