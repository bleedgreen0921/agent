"""PostgreSQL leased document worker; run with python -m rag_service.worker."""

import logging
import multiprocessing as mp
import os
import queue
import signal
import time
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from db.connection import connect
from db.config_check import validate_role
from db.worker_status import beat, remove
from rag_service.chunking import embedding_input, make_chunks
from rag_service.models import ModelFailure, embed
from rag_service.parse import ParseFailure, parse_file


log = logging.getLogger(__name__)
LEASE_SECONDS = 60
POLL_SECONDS = 1
MAX_POLL_BACKOFF_SECONDS = 30
DB_FAILURES = (psycopg.OperationalError, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable)


def claim() -> dict | None:
    with connect("RAG_DATABASE_URL", profile="control") as conn:
        timed_out = conn.execute("""SELECT id,version_id FROM rag.processing_jobs
            WHERE status IN ('queued','retry','running') AND deadline_at<=now()
            FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if timed_out:
            conn.execute("""UPDATE rag.processing_jobs SET status='failed',error_code='PROCESSING_TIMEOUT',
                lease_token=NULL,leased_until=NULL,updated_at=now() WHERE id=%s""", (timed_out["id"],))
            conn.execute("UPDATE rag.document_versions SET status='failed',error_code='PROCESSING_TIMEOUT' WHERE id=%s", (timed_out["version_id"],))
        exhausted = conn.execute("""SELECT id,version_id FROM rag.processing_jobs
            WHERE status='running' AND leased_until<now() AND attempts>=3
            FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if exhausted:
            conn.execute("UPDATE rag.processing_jobs SET status='failed',error_code='WORKER_INTERRUPTED',lease_token=NULL,leased_until=NULL,updated_at=now() WHERE id=%s", (exhausted["id"],))
            conn.execute("UPDATE rag.document_versions SET status='failed',error_code='WORKER_INTERRUPTED' WHERE id=%s", (exhausted["version_id"],))
        row = conn.execute("""SELECT j.id,j.version_id,j.attempts FROM rag.processing_jobs j
            WHERE ((j.status IN ('queued','retry') AND j.available_at<=now())
               OR (j.status='running' AND j.leased_until<now() AND j.attempts<3))
              AND (j.deadline_at IS NULL OR j.deadline_at>now())
            ORDER BY j.available_at, j.id FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not row:
            return None
        token = uuid4()
        claimed = conn.execute("""UPDATE rag.processing_jobs SET status='running',attempts=attempts+1,
            lease_token=%s,leased_until=now()+interval '60 seconds',heartbeat_at=now(),updated_at=now(),
            deadline_at=COALESCE(deadline_at,now()+(%s * interval '1 second'))
            WHERE id=%s RETURNING deadline_at,EXTRACT(EPOCH FROM deadline_at-now()) AS remaining_seconds""",
            (token, int(os.environ.get("RAG_DOCUMENT_TIMEOUT_SECONDS", "14400")), row["id"])).fetchone()
        conn.execute("UPDATE rag.document_versions SET status='processing' WHERE id=%s", (row["version_id"],))
        return {"id": row["id"], "version_id": row["version_id"], "token": token,
                "attempts": row["attempts"] + 1, "deadline_at": claimed["deadline_at"],
                "remaining_seconds": float(claimed["remaining_seconds"])}


def heartbeat(job: dict) -> bool:
    with connect("RAG_DATABASE_URL", profile="control") as conn:
        row = conn.execute("""UPDATE rag.processing_jobs SET leased_until=now()+interval '60 seconds',heartbeat_at=now()
            WHERE id=%s AND lease_token=%s AND status='running' AND deadline_at>now()
            RETURNING id""", (job["id"], job["token"])).fetchone()
    return bool(row)


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
    with connect("RAG_DATABASE_URL", profile="bulk") as conn:
        # The same lock is used by shadow switching and document publication.
        conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true FOR UPDATE")
        current = conn.execute("SELECT id FROM rag.index_revisions WHERE state IN ('active','building')").fetchall()
        if {item["id"] for item in current} != {revision["id"] for revision, _ in prepared}:
            raise ModelFailure("INDEX_CONFIGURATION_CHANGED")
        conn.execute("SELECT id FROM rag.documents WHERE id=%s FOR UPDATE", (row["document_id"],))
        conn.execute("DELETE FROM rag.chunks WHERE version_id=%s", (job["version_id"],))
        for ordinal, draft in enumerate(drafts, 1):
            chunk_id = uuid4()
            conn.execute("""INSERT INTO rag.chunks(id,document_id,version_id,ordinal,content,heading_path,source_locator,table_html,search_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (chunk_id, row["document_id"], job["version_id"], ordinal, draft.content, Jsonb(list(draft.heading_path)), Jsonb(draft.locator), draft.table_html, sparse_terms(draft.content)))
            for revision, vectors in prepared:
                conn.execute("INSERT INTO rag.embeddings(chunk_id,revision_id,embedding) VALUES (%s,%s,%s::vector)", (chunk_id, revision["id"], vector_literal(vectors[ordinal - 1])))
        active_job = conn.execute("""SELECT id FROM rag.processing_jobs WHERE id=%s AND lease_token=%s
            AND status='running' AND leased_until>clock_timestamp() AND deadline_at>clock_timestamp()
            FOR UPDATE""", (job["id"], job["token"])).fetchone()
        if not active_job:
            raise ModelFailure("LEASE_LOST")
        conn.execute("""UPDATE rag.document_versions SET status='superseded' WHERE document_id=%s AND status='active'""", (row["document_id"],))
        conn.execute("""UPDATE rag.document_versions SET status='active',error_code=NULL,warnings=%s,activated_at=now()
            WHERE id=%s""", (Jsonb(warnings), job["version_id"]))
        conn.execute("UPDATE rag.documents SET active_version_id=%s WHERE id=%s", (job["version_id"], row["document_id"]))
        completed = conn.execute("""UPDATE rag.processing_jobs SET status='done',lease_token=NULL,leased_until=NULL,
            error_code=NULL,updated_at=clock_timestamp() WHERE id=%s AND lease_token=%s
            AND status='running' AND leased_until>clock_timestamp() AND deadline_at>clock_timestamp()
            RETURNING id""", (job["id"], job["token"])).fetchone()
        if not completed:
            raise ModelFailure("LEASE_LOST")


def sparse_terms(content: str) -> str:
    import jieba
    import re

    words = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+", content)
    return " ".join(part.lower() for word in words for part in jieba.cut(word) if part.strip())


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(value) for value in values) + "]"


def fail(job: dict, code: str, retryable: bool) -> None:
    with connect("RAG_DATABASE_URL", profile="control") as conn:
        row = conn.execute("""SELECT id,deadline_at<=clock_timestamp() AS expired FROM rag.processing_jobs WHERE id=%s AND lease_token=%s
            AND status='running' FOR UPDATE""", (job["id"], job["token"])).fetchone()
        if not row:
            return
        if row["expired"]:
            code, retryable = "PROCESSING_TIMEOUT", False
        retry = retryable and job["attempts"] < 3
        conn.execute("""UPDATE rag.processing_jobs SET status=%s,error_code=%s,lease_token=NULL,leased_until=NULL,
            available_at=CASE WHEN %s THEN now()+interval '10 seconds' ELSE available_at END,updated_at=now()
            WHERE id=%s""", ("retry" if retry else "failed", code, retry, job["id"]))
        conn.execute("UPDATE rag.document_versions SET status=%s,error_code=%s WHERE id=%s", ("pending" if retry else "failed", code, job["version_id"]))


def fail_safely(job: dict, code: str, retryable: bool) -> None:
    try:
        fail(job, code, retryable)
    except DB_FAILURES:
        # Leave the job leased; it will be reclaimed after the lease expires.
        log.exception("Document worker could not record job failure")


def child_main(job: dict, outcome: mp.Queue) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    try:
        process(job)
        outcome.put((None, False))
    except ParseFailure as exc:
        outcome.put((str(exc), False))
    except ModelFailure as exc:
        outcome.put((exc.code, exc.retryable))
    except DB_FAILURES:
        log.exception("Document processing lost its database connection")
        outcome.put(("DATABASE_UNAVAILABLE", True))
    except Exception:
        log.exception("Document processing failed")
        outcome.put(("PROCESSING_FAILED", False))


def stop_process(process: mp.Process) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        return
    try:
        if hasattr(os, "killpg") and os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    process.join(timeout=10)
    if process.is_alive():
        try:
            if hasattr(os, "killpg") and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.join(timeout=2)


class Presence:
    def __init__(self):
        self.instance_id = uuid4()
        self.last_beat = float("-inf")
        self.last_pruned = float("-inf")

    def touch(self) -> None:
        now = time.monotonic()
        if now - self.last_beat < 10:
            return
        prune = now - self.last_pruned >= 21600
        beat("rag", self.instance_id, prune=prune)
        self.last_beat = now
        if prune:
            self.last_pruned = now


def supervise(job: dict, presence: Presence, *, child_target=child_main) -> None:
    context = mp.get_context("spawn")
    outcome = context.Queue(maxsize=1)
    child = context.Process(target=child_target, args=(job, outcome), name=f"document-{job['id']}")
    child.start()
    started = time.monotonic()
    deadline = started + job["remaining_seconds"]
    last_lease = started
    next_lease = started + 10
    try:
        while child.is_alive():
            now = time.monotonic()
            if now >= deadline:
                stop_process(child)
                fail_safely(job, "PROCESSING_TIMEOUT", False)
                return
            try:
                presence.touch()
            except DB_FAILURES:
                log.exception("Document worker heartbeat failed")
            if now >= next_lease:
                try:
                    if not heartbeat(job):
                        stop_process(child)
                        fail_safely(job, "LEASE_LOST", True)
                        return
                    last_lease = time.monotonic()
                    next_lease = last_lease + 10
                except DB_FAILURES:
                    log.exception("Document job heartbeat failed")
                    next_lease = time.monotonic() + 5
            if time.monotonic() - last_lease >= 40:
                stop_process(child)
                fail_safely(job, "DATABASE_UNAVAILABLE", True)
                return
            child.join(timeout=min(1, max(0.01, deadline - time.monotonic())))
        child.join(timeout=0)
        try:
            code, retryable = outcome.get(timeout=2)
        except queue.Empty:
            code, retryable = "WORKER_INTERRUPTED", True
        if code:
            fail_safely(job, code, retryable)
    finally:
        if child.is_alive():
            stop_process(child)
        outcome.close()
        outcome.join_thread()


def run_once(presence: Presence) -> bool:
    job = claim()
    if not job:
        return False
    supervise(job, presence)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    validate_role("rag-worker")
    presence = Presence()
    backoff = POLL_SECONDS
    try:
        while True:
            try:
                presence.touch()
                worked = run_once(presence)
            except DB_FAILURES:
                log.exception("Document worker polling lost its database connection")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_POLL_BACKOFF_SECONDS)
                continue
            backoff = POLL_SECONDS
            if not worked:
                time.sleep(POLL_SECONDS)
    finally:
        try:
            remove("rag", presence.instance_id)
        except DB_FAILURES:
            log.warning("Could not remove document worker heartbeat")


if __name__ == "__main__":
    main()
