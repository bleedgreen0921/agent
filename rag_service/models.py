import math
import os
import httpx
from execution.flow import DownstreamLimited
from execution.scope import operation_deadline
from execution.persistence import resources
from execution.tokens import InputTooLarge, TokenBudgetExceeded

from execution.providers import ProviderFailure as ModelFailure, embedding, check_response as _check_response

async def embed(texts: list[str], model: str, dimensions: int) -> list[list[float]]:
    return await embedding(texts, url=os.environ.get('RAG_EMBEDDING_URL', ''), model=model,
        dimensions=dimensions, key=os.environ.get('RAG_EMBEDDING_KEY'),
        seconds=float(os.environ.get('RAG_MODEL_TIMEOUT_SECONDS', '30')))

async def rerank(query: str, contents: list[str]) -> list[dict]:
    url = os.environ.get('RAG_RERANK_URL', '').rstrip('/')
    if not url:
        raise ModelFailure('RERANK_UNAVAILABLE')
    try:
        response = await resources().http.request('rerank', 'POST', url + '/rerank', deadline=await operation_deadline(float(os.environ.get('RAG_MODEL_TIMEOUT_SECONDS', '30'))), json={'model': os.environ.get('RAG_RERANK_MODEL', 'bge-reranker-v2-m3'), 'query': query, 'top_n': len(contents), 'documents': contents}, headers=_auth_header('RAG_RERANK_KEY'))
        _check_response(response, 'RERANK')
        results = response.json()['results']
        if len(results) != len(contents):
            raise ValueError
        indices = [item['index'] for item in results]
        if any(type(index) is not int for index in indices) or set(indices) != set(range(len(contents))) or any((type(item['relevance_score']) not in (int, float) or not math.isfinite(item['relevance_score']) for item in results)):
            raise ValueError
        return [{'index': item['index'], 'score': item['relevance_score']} for item in sorted(results, key=lambda item: -item['relevance_score'])]
    except ModelFailure:
        raise
    except TokenBudgetExceeded as exc:
        raise ModelFailure('TOKEN_BUDGET_EXHAUSTED', False) from exc
    except InputTooLarge as exc:
        raise ModelFailure('RERANK_INPUT_TOO_LARGE', False) from exc
    except DownstreamLimited as exc:
        raise ModelFailure("RERANK_RATE_LIMITED", True, exc.retry_after) from exc
    except (httpx.TransportError,TimeoutError) as exc:
        raise ModelFailure('RERANK_UNAVAILABLE') from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelFailure('RERANK_RESPONSE_INVALID') from exc

async def rewrite(query: str) -> str:
    url = os.environ.get('RAG_REWRITE_URL', '').rstrip('/')
    if not url:
        raise ModelFailure('QUERY_REWRITE_FALLBACK')
    try:
        response = await resources().http.request('rewrite', 'POST', url + '/v1/chat/completions', deadline=await operation_deadline(float(os.environ.get('RAG_MODEL_TIMEOUT_SECONDS', '30'))), json={'model': os.environ.get('RAG_REWRITE_MODEL', ''), 'messages': [{'role': 'system', 'content': 'Rewrite the query for semantic retrieval. Preserve entities and constraints. Return only the query.'}, {'role': 'user', 'content': query}], 'temperature': 0}, headers=_auth_header('RAG_REWRITE_KEY'))
        _check_response(response, 'QUERY_REWRITE')
        result = response.json()['choices'][0]['message']['content'].strip()
        if not result:
            raise ValueError
        return result
    except (ModelFailure, DownstreamLimited) as exc:
        raise ModelFailure(getattr(exc, "code", "QUERY_REWRITE_FALLBACK"), True, getattr(exc, "retry_after", None)) from exc
    except TokenBudgetExceeded as exc:
        raise ModelFailure('TOKEN_BUDGET_EXHAUSTED', False) from exc
    except (ValueError, KeyError, TypeError, httpx.HTTPError, TimeoutError) as exc:
        raise ModelFailure('QUERY_REWRITE_FALLBACK') from exc

def _auth_header(env: str) -> dict[str, str]:
    key = os.environ.get(env)
    return {'Authorization': 'Bearer ' + key} if key else {}
