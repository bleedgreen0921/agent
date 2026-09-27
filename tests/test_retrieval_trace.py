"""Candidate traces describe the bounded search work without storing content."""

import json
from contextlib import nullcontext
from uuid import uuid4

import pytest

from contracts.errors import ApiError
from rag_service import retrieval
from rag_service.models import ModelFailure


def _row(index: int, **scores):
    return {"id": uuid4(), "document_id": uuid4(), "version_id": uuid4(),
            "content": f"SECRET_CONTENT_{index}", "title": f"SECRET_TITLE_{index}",
            "source_locator": {}, **scores}


def _search(monkeypatch, dense, sparse, ranked=None, *, top_k=2):
    audits = []
    monkeypatch.setattr(retrieval, "connect", lambda _: nullcontext())
    monkeypatch.setattr(retrieval, "_revision", lambda _: {"id": uuid4(), "model": "mock", "dimensions": 2})
    monkeypatch.setattr(retrieval, "rewrite", lambda _: "SECRET_REWRITE")
    monkeypatch.setattr(retrieval, "embed", lambda *_: [[0.1, 0.2]])
    monkeypatch.setattr(retrieval, "_dense", lambda *_: dense)
    monkeypatch.setattr(retrieval, "_sparse", lambda *_: sparse)
    monkeypatch.setattr(retrieval, "rerank", lambda *_: ranked)
    monkeypatch.setattr(retrieval, "_audit", lambda *args, **kwargs: audits.append(kwargs))
    result = retrieval.search(retrieval.EvidenceSearchRequest(query="SECRET_QUERY", top_k=top_k), uuid4(), "run", "tool")
    return result, audits[0]


def test_complete_candidate_order_scores_and_selection(monkeypatch):
    first, shared, sparse_only = _row(1, distance=0.1), _row(2, distance=0.2, score=0.8), _row(3, score=0.6)
    ranked = [{"index": 2, "score": 0.9}, {"index": 0, "score": 0.5}, {"index": 1, "score": 0.1}]
    result, audit = _search(monkeypatch, [first, shared], [shared, sparse_only], ranked)
    stages = audit["candidate_trace"]["stages"]
    eid = lambda row: "ev_" + str(row["id"])
    assert audit["candidate_trace"]["top_k"] == 2
    assert stages["dense"]["candidates"] == [
        {"evidence_id": eid(first), "rank": 1, "distance": 0.1},
        {"evidence_id": eid(shared), "rank": 2, "distance": 0.2}]
    assert stages["fts"]["candidates"] == [
        {"evidence_id": eid(shared), "rank": 1, "score": 0.8},
        {"evidence_id": eid(sparse_only), "rank": 2, "score": 0.6}]
    assert [item["evidence_id"] for item in stages["fusion"]["candidates"]] == [eid(shared), eid(first), eid(sparse_only)]
    assert stages["fusion"]["candidates"][0]["rrf_score"] == pytest.approx(1 / 61 + 1 / 62)
    assert stages["rerank"]["input_evidence_ids"] == [eid(shared), eid(first), eid(sparse_only)]
    assert stages["rerank"]["candidates"] == [
        {"evidence_id": eid(sparse_only), "rank": 1, "score": 0.9},
        {"evidence_id": eid(shared), "rank": 2, "score": 0.5},
        {"evidence_id": eid(first), "rank": 3, "score": 0.1}]
    assert audit["selected_evidence_ids"] == [eid(sparse_only), eid(shared)]
    assert [item["evidence_id"] for item in result["evidences"]] == audit["selected_evidence_ids"]
    encoded = json.dumps(audit["candidate_trace"])
    assert all(secret not in encoded for secret in ("SECRET_QUERY", "SECRET_REWRITE", "SECRET_CONTENT", "SECRET_TITLE"))


def test_no_hits_leave_rerank_unexecuted(monkeypatch):
    result, audit = _search(monkeypatch, [], [], None)
    assert result["status"] == "no_hits"
    assert audit["candidate_trace"]["stages"]["rerank"]["status"] == "not_executed"
    assert audit["candidate_trace"]["stages"]["fusion"]["candidates"] == []



def test_fusion_keeps_all_candidates_before_rerank_limit(monkeypatch):
    dense = [_row(index, distance=index / 50) for index in range(50)]
    sparse = [_row(index + 50, score=(50 - index) / 50) for index in range(50)]
    ranked = [{"index": index, "score": float(40 - index)} for index in range(40)]
    _, audit = _search(monkeypatch, dense, sparse, ranked)
    stages = audit["candidate_trace"]["stages"]
    assert len(stages["dense"]["candidates"]) == 50
    assert len(stages["fts"]["candidates"]) == 50
    assert len(stages["fusion"]["candidates"]) == 100
    assert len(stages["rerank"]["input_evidence_ids"]) == 40
    assert len(stages["rerank"]["candidates"]) == 40


def test_degraded_and_failed_stages_preserve_completed_work(monkeypatch):
    row = _row(1, distance=0.1, score=0.5)
    audits = []
    monkeypatch.setattr(retrieval, "connect", lambda _: nullcontext())
    monkeypatch.setattr(retrieval, "_revision", lambda _: {"id": uuid4(), "model": "mock", "dimensions": 2})
    monkeypatch.setattr(retrieval, "rewrite", lambda _: "rewrite")
    monkeypatch.setattr(retrieval, "embed", lambda *_: [[0.1, 0.2]])
    monkeypatch.setattr(retrieval, "_dense", lambda *_: [row])
    monkeypatch.setattr(retrieval, "_sparse", lambda *_: (_ for _ in ()).throw(retrieval.psycopg.OperationalError()))
    monkeypatch.setattr(retrieval, "rerank", lambda *_: (_ for _ in ()).throw(ModelFailure("RERANK_UNAVAILABLE")))
    monkeypatch.setattr(retrieval, "_audit", lambda *args, **kwargs: audits.append(kwargs))
    result = retrieval.search(retrieval.EvidenceSearchRequest(query="query"), uuid4(), "run", "tool")
    stages = audits[-1]["candidate_trace"]["stages"]
    assert result["status"] == "degraded"
    assert result["degradations"] == ["FTS_UNAVAILABLE", "RERANK_UNAVAILABLE"]
    assert stages["dense"]["status"] == "success" and len(stages["dense"]["candidates"]) == 1
    assert stages["fts"]["status"] == "unavailable"
    assert stages["rerank"]["status"] == "unavailable"
    assert stages["rerank"]["input_evidence_ids"] == ["ev_" + str(row["id"])]

    monkeypatch.setattr(retrieval, "embed", lambda *_: (_ for _ in ()).throw(ModelFailure("EMBEDDING_UNAVAILABLE")))
    monkeypatch.setattr(retrieval, "_sparse", lambda *_: [row])
    monkeypatch.setattr(retrieval, "rerank", lambda *_: [{"index": 0, "score": 0.8}])
    sparse_result = retrieval.search(retrieval.EvidenceSearchRequest(query="query"), uuid4(), "run", "tool")
    sparse_stages = audits[-1]["candidate_trace"]["stages"]
    assert sparse_result["status"] == "degraded"
    assert sparse_result["degradations"] == ["DENSE_UNAVAILABLE"]
    assert sparse_stages["dense"]["status"] == "unavailable"
    assert sparse_stages["fts"]["status"] == "success"

    monkeypatch.setattr(retrieval, "embed", lambda *_: [[0.1, 0.2]])
    monkeypatch.setattr(retrieval, "_sparse", lambda *_: (_ for _ in ()).throw(retrieval.psycopg.OperationalError()))
    monkeypatch.setattr(retrieval, "rerank", lambda *_: (_ for _ in ()).throw(ModelFailure("RERANK_AUTH_FAILED", False)))
    with pytest.raises(ApiError):
        retrieval.search(retrieval.EvidenceSearchRequest(query="query"), uuid4(), "run", "tool")
    assert audits[-1]["error_code"] == "RERANK_AUTH_FAILED"
    assert audits[-1]["candidate_trace"]["stages"]["fusion"]["status"] == "success"

    monkeypatch.setattr(retrieval, "embed", lambda *_: (_ for _ in ()).throw(ModelFailure("EMBEDDING_NOT_CONFIGURED", False)))
    with pytest.raises(ApiError):
        retrieval.search(retrieval.EvidenceSearchRequest(query="query"), uuid4(), "run", "tool")
    stages = audits[-1]["candidate_trace"]["stages"]
    assert stages["rewrite"]["status"] == "success"
    assert stages["dense"]["status"] == "unavailable"
    assert stages["fts"]["status"] == "not_executed"
    assert stages["fusion"]["status"] == "not_executed"
