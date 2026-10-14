from rag_service.storage import retrieval as retrieval_store
from rag_service.storage.retrieval import _audit,_evidence,read_evidence
import hashlib
from uuid import UUID, uuid4
from contracts.errors import ApiError
from contracts.v1 import ErrorCode, EvidenceSearchRequest
from rag_service.models import ModelFailure, embed, rerank, rewrite
from rag_service.computation import sparse_terms
import asyncio
from execution.persistence import resources, deadline
from execution.scope import work_owner

def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

async def search(request: EvidenceSearchRequest, run_id: str, tool_call_id: str, *, allocation=None) -> dict:
    from execution.infra.http import fail_fast_pressure
    marker = fail_fast_pressure.set(True)
    from execution import tokens,delegation,timing
    from uuid import uuid5,NAMESPACE_URL
    identity=UUID(allocation['id']) if allocation else uuid4()
    key=None
    try:
        existing=await delegation.start(identity,run_id,tool_call_id,request.model_dump(),allocation)
        if existing is not None:return existing
        key=tokens.budget_context.set({'id':str(identity),'kind':'search','run_id':run_id,'tool_call_id':tool_call_id,
            'limit':allocation['limit'] if allocation else None,'expires_at':allocation['expires_at'] if allocation else None})
        with timing.capture() as measured:
            try:result=await _search(request,run_id,tool_call_id)
            except BaseException as exc:
                if not measured.get('audited'):
                    try:
                        await _audit('rag_failed_'+uuid4().hex,run_id,tool_call_id,None,'unavailable',[],operation='search',
                            error_code=type(exc).__name__,candidate_trace={'version':2,'timing':timing.snapshot(),'stages':measured.get('stages',{})})
                    except BaseException:
                        import logging
                        logging.getLogger(__name__).warning('Interrupted retrieval audit incomplete')
                if isinstance(exc,(ApiError,ModelFailure,tokens.TokenBudgetExceeded)):
                    await delegation.finish(identity,{'usage':await tokens.report(identity),'error_type':type(exc).__name__},status='failed')
                raise
        result['usage']=await tokens.report(identity)
        await delegation.finish(identity,result)
        return result
    finally:
        if key is not None:tokens.budget_context.reset(key)
        fail_fast_pressure.reset(marker)


async def _search(request: EvidenceSearchRequest, run_id: str, tool_call_id: str) -> dict:
    request_id, retrieval_id = ('rag_req_' + uuid4().hex, 'ret_' + uuid4().hex)
    degradations: list[str] = []
    query_sha256 = _sha256(request.query)
    rewritten_query_sha256 = query_sha256
    stages = {name: {'status': 'not_executed', 'candidates': []} for name in ('dense', 'fts', 'fusion', 'rerank')}
    stages['rewrite'] = {'status': 'not_executed'}
    stages['rerank']['input_evidence_ids'] = []
    candidate_trace = {'version': 2, 'top_k': request.top_k, 'stages': stages}
    from execution.timing import collector
    collector.get()['stages']=stages

    async def audit(status: str, revision_id: UUID | None, selected: list[str], error_code: str | None=None) -> None:
        from execution.timing import snapshot
        candidate_trace['timing']=snapshot()
        collector.get()['audited']=True
        await _audit(request_id, run_id, tool_call_id, revision_id, status, degradations, operation='search', retrieval_id=retrieval_id, query_sha256=query_sha256, rewritten_query_sha256=rewritten_query_sha256, selected_evidence_ids=selected, error_code=error_code, candidate_trace=candidate_trace)
    try:
        from execution.timing import span
        with span('index_read'):revision = await retrieval_store.revision()
    except ApiError:
        await audit('unavailable', None, [], 'INDEX_UNAVAILABLE')
        raise

    async def prepare_vector():
        nonlocal rewritten_query_sha256
        try:
            with span('rewrite'):dense_query = await rewrite(request.query)
            rewritten_query_sha256 = _sha256(dense_query)
            stages['rewrite']['status'] = 'success'
        except ModelFailure as exc:
            stages['rewrite']['error_code'] = exc.code
            dense_query = request.query
            degradations.append('QUERY_REWRITE_FALLBACK')
            stages['rewrite']['status'] = 'unavailable'
        try:
            with span('embedding'):
                return (await embed([f'Instruct: Given a user question, retrieve relevant passages that answer the question\nQuery: {dense_query}'], revision['model'], revision['dimensions']))[0]
        except ModelFailure as exc:
            stages['dense']['status'] = 'unavailable'
            stages['dense']['error_code'] = exc.code
            if not exc.retryable and exc.code not in ('TOKEN_BUDGET_EXHAUSTED','EMBEDDING_INPUT_TOO_LARGE'):
                await audit('unavailable', revision['id'], [], exc.code)
                raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, 'Embedding configuration unavailable') from exc
            return None

    async def prepare_terms():
        with span('sparse_prepare'):
            return await resources().cpu.run(sparse_terms, request.query, owner=work_owner(), deadline=deadline(120))
    try:
        async with asyncio.TaskGroup() as group:
            dense_task = group.create_task(prepare_vector())
            sparse_task = group.create_task(prepare_terms())
    except ExceptionGroup as exc:
        from execution.errors import select_failure
        raise select_failure(exc) from exc
    try:
        with span('recall_transaction'):
            snapshot = await retrieval_store.recall(revision, dense_task.result(), sparse_task.result())
    except ApiError:
        await audit('unavailable', revision['id'], [], 'INDEX_CONFIGURATION_INVALID')
        raise
    revision, dense, sparse = snapshot.revision, snapshot.dense, snapshot.sparse
    for name, results, metric, code in (
        ('dense', dense, 'distance', 'DENSE_UNAVAILABLE'),
        ('fts', sparse, 'score', 'FTS_UNAVAILABLE'),
    ):
        if results is None:
            stages[name]['status'] = 'unavailable'
            degradations.append(code)
        else:
            stages[name] = {'status': 'success', 'candidates': [
                {'evidence_id': 'ev_' + str(row['id']), 'rank': rank, metric: row[metric]}
                for rank, row in enumerate(results, 1)]}
    if dense is None and sparse is None:
        await audit('unavailable', revision['id'], [], 'RETRIEVAL_UNAVAILABLE')
        raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, 'Retrieval unavailable')
    with span('fusion'):
        scores: dict[UUID, float] = {}
        rows: dict[UUID, dict] = {}
        for results in (dense or [], sparse or []):
            for rank, row in enumerate(results, 1):
                rows[row['id']] = row
                scores[row['id']] = scores.get(row['id'], 0) + 1 / (60 + rank)
        fused = sorted(rows, key=lambda item: (-scores[item], str(item)))
        stages['fusion'] = {'status': 'success', 'candidates': [{'evidence_id': 'ev_' + str(item), 'rank': rank, 'rrf_score': scores[item]} for rank, item in enumerate(fused, 1)]}
    ordered = fused[:40]
    if ordered:
        stages['rerank']['input_evidence_ids'] = ['ev_' + str(item) for item in ordered]
        try:
            with span('rerank'):ranked = await rerank(request.query, [rows[item]['content'] for item in ordered])
            stages['rerank']['status'] = 'success'
            stages['rerank']['candidates'] = [{'evidence_id': 'ev_' + str(ordered[item['index']]), 'rank': rank, 'score': item['score']} for rank, item in enumerate(ranked, 1)]
            ordered = [ordered[item['index']] for item in ranked]
        except ModelFailure as exc:
            stages['rerank']['status'] = 'unavailable'
            stages['rerank']['error_code'] = exc.code
            if not exc.retryable and exc.code not in ('TOKEN_BUDGET_EXHAUSTED','RERANK_INPUT_TOO_LARGE'):
                await audit('unavailable', revision['id'], [], exc.code)
                raise ApiError(503, ErrorCode.RAG_UNAVAILABLE, 'Reranker configuration unavailable') from exc
            degradations.append('RERANK_UNAVAILABLE')
    order=['QUERY_REWRITE_FALLBACK','DENSE_UNAVAILABLE','FTS_UNAVAILABLE','RERANK_UNAVAILABLE']
    degradations[:]=[code for code in order if code in degradations]
    with span('response_assembly'):
        selected = [_evidence(rows[item], rank) for rank, item in enumerate(ordered[:request.top_k], 1)]
    status = 'no_hits' if not selected else 'degraded' if degradations else 'ok'
    await audit(status, revision['id'], [item['evidence_id'] for item in selected])
    return {'request_id': request_id, 'retrieval_id': retrieval_id, 'status': status, 'evidences': selected, 'degradations': degradations}
