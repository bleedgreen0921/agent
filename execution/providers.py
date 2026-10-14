"""Shared native HTTP provider validation, independent of business pipelines."""
import json
import math
import httpx
from execution.flow import DownstreamLimited
from execution.persistence import resources
from execution.scope import operation_deadline, work_owner


class ProviderFailure(Exception):
    def __init__(self, code, retryable=True, retry_after=None):
        self.code, self.retryable, self.retry_after = code, retryable, retry_after
        super().__init__(code)


def check_response(response, prefix):
    if response.status_code == 429:
        raise ProviderFailure(prefix + "_RATE_LIMITED", True, response.headers.get("Retry-After"))
    if response.status_code in (401, 403):
        raise ProviderFailure(prefix + '_AUTH_FAILED', False)
    if response.status_code in (400, 413, 422):
        raise ProviderFailure(prefix + '_INVALID_INPUT', False)
    if response.status_code >= 400:
        raise ProviderFailure(prefix + '_UNAVAILABLE')


def embedding_vectors(content, count, dimensions=None):
    data = json.loads(content)['data']
    if not isinstance(data, list) or len(data) != count:
        raise ValueError('Embedding response count differs from input')
    indexed = {}
    for item in data:
        index, vector = item['index'], item['embedding']
        if type(index) is not int or index not in range(count) or index in indexed:
            raise ValueError('Embedding indices must be complete and unique')
        if not isinstance(vector, list) or not vector or len(vector) > 16000:
            raise ValueError('Invalid embedding dimension')
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions or any(type(n) not in (int, float) or not math.isfinite(n) for n in vector):
            raise ValueError('Invalid embedding values or inconsistent dimensions')
        indexed[index] = vector
    return [indexed[i] for i in range(count)]


async def embedding(texts, *, url, model, key=None, seconds=30, dimensions=None):
    if not url:
        raise ProviderFailure('EMBEDDING_NOT_CONFIGURED', False)
    limit = await operation_deadline(seconds)
    from execution.tokens import envelope,InputTooLarge,TokenBudgetExceeded
    try:await envelope('embedding',{'model':model,'input':texts})
    except InputTooLarge as exc:
        if len(texts)<2:raise ProviderFailure('EMBEDDING_INPUT_TOO_LARGE',False) from exc
        middle=len(texts)//2
        from execution.persistence import call_scope
        with call_scope(limit):
            left=await embedding(texts[:middle],url=url,model=model,key=key,seconds=seconds,dimensions=dimensions)
            right=await embedding(texts[middle:],url=url,model=model,key=key,seconds=seconds,dimensions=dimensions)
        return left+right
    base = url.rstrip('/')
    endpoint = base + ('/embeddings' if base.endswith('/v1') else '/v1/embeddings')
    try:
        response = await resources().http.request('embedding', 'POST', endpoint, deadline=limit,
            json={'model': model, 'input': texts}, headers={'Authorization': 'Bearer ' + key} if key else {})
        check_response(response, 'EMBEDDING')
        return await resources().io.run(embedding_vectors, response.content, len(texts), dimensions,
                                       owner=work_owner(), deadline=limit)
    except ProviderFailure:
        raise
    except TokenBudgetExceeded as exc:
        raise ProviderFailure('TOKEN_BUDGET_EXHAUSTED',False) from exc
    except DownstreamLimited as exc:
        raise ProviderFailure("EMBEDDING_RATE_LIMITED", True, exc.retry_after) from exc
    except (httpx.TransportError, TimeoutError) as exc:
        raise ProviderFailure('EMBEDDING_UNAVAILABLE') from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderFailure('EMBEDDING_RESPONSE_INVALID', False) from exc
