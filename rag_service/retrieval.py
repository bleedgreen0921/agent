import hashlib
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from contracts.errors import ApiError
from contracts.v1 import ErrorCode, EvidenceSearchRequest
from db.connection import connect
from rag_service.models import ModelFailure, embed, rerank, rewrite
from rag_service.worker import sparse_terms, vector_literal


VISIBLE = """d.deleted_at IS NULL AND d.active_version_id=c.version_id
    AND (d.visibility='public' OR EXISTS (
      SELECT 1 FROM rag.document_access a WHERE a.document_id=d.id AND a.team_id=%s))"""
VISIBLE_HISTORY = """d.deleted_at IS NULL AND (d.visibility='public' OR EXISTS (
      SELECT 1 FROM rag.document_access a WHERE a.document_id=d.id AND a.team_id=%s))"""


def _revision(conn) -> dict:
    row = conn.execute("""SELECT r.id,r.model,r.dimensions FROM rag.index_state s
        JOIN rag.index_revisions r ON r.id=s.active_revision_id WHERE s.singleton=true AND r.state='active'""").fetchone()
    if not row:
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, "Index unavailable")
    return row


def _dense(conn, team_id: UUID, revision: dict, vector: list[float]) -> list[dict]:
    dimension = int(revision["dimensions"])
    if not 1 <= dimension <= 4096:
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, "Index configuration invalid")
    query = f"""SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title
        FROM rag.embeddings e JOIN rag.chunks c ON c.id=e.chunk_id
        JOIN rag.documents d ON d.id=c.document_id
        WHERE e.revision_id=%s AND {VISIBLE}
        ORDER BY e.embedding::vector({dimension}) <=> %s::vector({dimension}), c.id LIMIT 50"""
    return conn.execute(query, (revision["id"], team_id, vector_literal(vector))).fetchall()


def _sparse(conn, team_id: UUID, query: str) -> list[dict]:
    terms = sparse_terms(query)
    if not terms:
        return []
    return conn.execute(f"""SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title
        FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id
        WHERE {VISIBLE} AND c.search_vector @@ plainto_tsquery('simple',%s)
        ORDER BY ts_rank_cd(c.search_vector,plainto_tsquery('simple',%s)) DESC,c.id LIMIT 50""", (team_id, terms, terms)).fetchall()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit(
    request_id: str,
    run_id: str,
    tool_call_id: str,
    team_id: UUID,
    revision_id: UUID | None,
    status: str,
    degradations: list[str],
    *,
    operation: str,
    evidence_id: UUID | None = None,
    retrieval_id: str | None = None,
    query_sha256: str | None = None,
    rewritten_query_sha256: str | None = None,
    selected_evidence_ids: list[str] | None = None,
    error_code: str | None = None,
) -> None:
    with connect("RAG_DATABASE_URL") as conn:
        conn.execute("""INSERT INTO rag.retrieval_audit(
            id,request_id,run_id,tool_call_id,team_id,revision_id,status,degradations,evidence_id,
            operation,retrieval_id,query_sha256,rewritten_query_sha256,selected_evidence_ids,error_code)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                uuid4(), request_id, run_id, tool_call_id, team_id, revision_id, status,
                Jsonb(degradations), evidence_id, operation, retrieval_id, query_sha256,
                rewritten_query_sha256,
                Jsonb(selected_evidence_ids) if selected_evidence_ids is not None else None,
                error_code,
            ))


def _evidence(row: dict, rank: int | None = None) -> dict:
    result = {"evidence_id": "ev_" + str(row["id"]), "document_id": "doc_" + str(row["document_id"]), "document_version_id": "ver_" + str(row["version_id"]), "title": row["title"], "content": row["content"], "source_locator": row["source_locator"]}
    if rank is not None:
        result["rank"] = rank
    return result


def search(request: EvidenceSearchRequest, team_id: UUID, run_id: str, tool_call_id: str) -> dict:
    request_id, retrieval_id = "rag_req_" + uuid4().hex, "ret_" + uuid4().hex
    degradations: list[str] = []
    query_sha256 = _sha256(request.query)
    rewritten_query_sha256 = query_sha256

    def audit(status: str, revision_id: UUID | None, selected: list[str], error_code: str | None = None) -> None:
        _audit(
            request_id,
            run_id,
            tool_call_id,
            team_id,
            revision_id,
            status,
            degradations,
            operation="search",
            retrieval_id=retrieval_id,
            query_sha256=query_sha256,
            rewritten_query_sha256=rewritten_query_sha256,
            selected_evidence_ids=selected,
            error_code=error_code,
        )

    try:
        with connect("RAG_DATABASE_URL") as conn:
            revision = _revision(conn)
    except ApiError:
        audit("unavailable", None, [], "INDEX_UNAVAILABLE")
        raise
    try:
        dense_query = rewrite(request.query)
        rewritten_query_sha256 = _sha256(dense_query)
    except ModelFailure:
        dense_query = request.query
        degradations.append("QUERY_REWRITE_FALLBACK")
    try:
        vector = embed([f"Instruct: Given a user question, retrieve relevant passages that answer the question\nQuery: {dense_query}"], revision["model"], revision["dimensions"])[0]
        with connect("RAG_DATABASE_URL") as conn:
            dense = _dense(conn, team_id, revision, vector)
    except ModelFailure as exc:
        if not exc.retryable:
            audit("unavailable", revision["id"], [], exc.code)
            raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, "Embedding configuration unavailable") from exc
        dense = None
        degradations.append("DENSE_UNAVAILABLE")
    except ApiError:
        audit("unavailable", revision["id"], [], "INDEX_CONFIGURATION_INVALID")
        raise
    except psycopg.OperationalError:
        dense = None
        degradations.append("DENSE_UNAVAILABLE")
    try:
        with connect("RAG_DATABASE_URL") as conn:
            sparse = _sparse(conn, team_id, request.query)
    except psycopg.OperationalError:
        sparse = None
        degradations.append("FTS_UNAVAILABLE")
    if dense is None and sparse is None:
        audit("unavailable", revision["id"], [], "RETRIEVAL_UNAVAILABLE")
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, "Retrieval unavailable")
    scores: dict[UUID, float] = {}
    rows: dict[UUID, dict] = {}
    for results in (dense or [], sparse or []):
        for rank, row in enumerate(results, 1):
            rows[row["id"]] = row
            scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank)
    ordered = sorted(rows, key=lambda item: (-scores[item], str(item)))[:40]
    if ordered:
        try:
            indices = rerank(request.query, [rows[item]["content"] for item in ordered])
            ordered = [ordered[index] for index in indices]
        except ModelFailure as exc:
            if not exc.retryable:
                audit("unavailable", revision["id"], [], exc.code)
                raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, "Reranker configuration unavailable") from exc
            degradations.append("RERANK_UNAVAILABLE")
    selected = [_evidence(rows[item], rank) for rank, item in enumerate(ordered[:request.top_k], 1)]
    status = "no_hits" if not selected else "degraded" if degradations else "ok"
    audit(status, revision["id"], [item["evidence_id"] for item in selected])
    return {"request_id": request_id, "retrieval_id": retrieval_id, "status": status, "evidences": selected, "degradations": degradations}


def read_evidence(evidence_id: str, team_id: UUID, run_id: str, tool_call_id: str) -> dict:
    request_id = "rag_req_" + uuid4().hex
    try:
        chunk_id = UUID(evidence_id.removeprefix("ev_")) if evidence_id.startswith("ev_") else None
    except ValueError:
        chunk_id = None
    with connect("RAG_DATABASE_URL") as conn:
        state = conn.execute("SELECT active_revision_id FROM rag.index_state WHERE singleton=true").fetchone()
        row = conn.execute(f"""SELECT c.id,c.document_id,c.version_id,c.content,c.source_locator,d.title
            FROM rag.chunks c JOIN rag.documents d ON d.id=c.document_id
            WHERE c.id=%s AND {VISIBLE_HISTORY}""", (chunk_id, team_id)).fetchone() if chunk_id else None
    _audit(
        request_id,
        run_id,
        tool_call_id,
        team_id,
        state["active_revision_id"] if state else None,
        "read_ok" if row else "not_found",
        [],
        operation="read",
        evidence_id=chunk_id,
        selected_evidence_ids=["ev_" + str(chunk_id)] if row else [],
        error_code=None if row else "EVIDENCE_NOT_FOUND",
    )
    if not row:
        raise ApiError(404, ErrorCode.NOT_FOUND, "Evidence not found")
    return _evidence(row)
