import math
import os

import httpx


class ModelFailure(Exception):
    def __init__(self, code: str, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _client() -> httpx.Client:
    return httpx.Client(timeout=float(os.environ.get("RAG_MODEL_TIMEOUT_SECONDS", "30")))


def embed(texts: list[str], model: str, dimensions: int) -> list[list[float]]:
    url = os.environ.get("RAG_EMBEDDING_URL", "").rstrip("/")
    if not url:
        raise ModelFailure("EMBEDDING_NOT_CONFIGURED", False)
    try:
        with _client() as client:
            response = client.post(url + "/v1/embeddings", json={"model": model, "input": texts}, headers=_auth_header("RAG_EMBEDDING_KEY"))
        _check_response(response, "EMBEDDING")
        data = response.json()["data"]
        indexed = {item["index"]: item["embedding"] for item in data}
        vectors = [indexed[i] for i in range(len(texts))]
        if any(len(vector) != dimensions or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector) for vector in vectors):
            raise ValueError
        return vectors
    except ModelFailure:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ModelFailure("EMBEDDING_UNAVAILABLE") from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelFailure("EMBEDDING_RESPONSE_INVALID", False) from exc


def rerank(query: str, contents: list[str]) -> list[int]:
    url = os.environ.get("RAG_RERANK_URL", "").rstrip("/")
    if not url:
        raise ModelFailure("RERANK_UNAVAILABLE")
    try:
        with _client() as client:
            response = client.post(url + "/rerank", json={"model": os.environ.get("RAG_RERANK_MODEL", "bge-reranker-v2-m3"), "query": query, "top_n": len(contents), "documents": contents}, headers=_auth_header("RAG_RERANK_KEY"))
        _check_response(response, "RERANK")
        results = response.json()["results"]
        if len(results) != len(contents):
            raise ValueError
        indices = [item["index"] for item in results]
        if set(indices) != set(range(len(contents))) or any(not math.isfinite(item["relevance_score"]) for item in results):
            raise ValueError
        return [item["index"] for item in sorted(results, key=lambda item: -item["relevance_score"])]
    except ModelFailure:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ModelFailure("RERANK_UNAVAILABLE") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelFailure("RERANK_RESPONSE_INVALID") from exc


def rewrite(query: str) -> str:
    url = os.environ.get("RAG_REWRITE_URL", "").rstrip("/")
    if not url:
        raise ModelFailure("QUERY_REWRITE_FALLBACK")
    try:
        with _client() as client:
            response = client.post(url + "/v1/chat/completions", json={"model": os.environ.get("RAG_REWRITE_MODEL", ""), "messages": [{"role": "system", "content": "Rewrite the query for semantic retrieval. Preserve entities and constraints. Return only the query."}, {"role": "user", "content": query}], "temperature": 0}, headers=_auth_header("RAG_REWRITE_KEY"))
        _check_response(response, "QUERY_REWRITE")
        result = response.json()["choices"][0]["message"]["content"].strip()
        if not result:
            raise ValueError
        return result
    except (ModelFailure, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        raise ModelFailure("QUERY_REWRITE_FALLBACK") from exc


def _auth_header(env: str) -> dict[str, str]:
    key = os.environ.get(env)
    return {"Authorization": "Bearer " + key} if key else {}


def _check_response(response: httpx.Response, prefix: str) -> None:
    if response.status_code in (401, 403):
        raise ModelFailure(prefix + "_AUTH_FAILED", False)
    if response.status_code in (400, 413, 422):
        raise ModelFailure(prefix + "_INVALID_INPUT", False)
    if response.status_code >= 400:
        raise ModelFailure(prefix + "_UNAVAILABLE")
