"""Real MVCC races: both recall branches must see one document/index view."""
import asyncio
import os
from uuid import uuid4

import psycopg
import pytest

from execution.persistence import resources, transaction, using
from rag_service import retrieval
from rag_service.computation import vector_literal
from rag_service.storage import retrieval as store
from tests.native_async import runtimes

pytestmark = pytest.mark.skipif(os.environ.get('PERSONAL_TEST_DATABASE') != '1', reason='dedicated PostgreSQL required')


async def _seed(*, model='Qwen3-Embedding-0.6B', dimensions=1024):
    doc, old_version, new_version, old_chunk, new_chunk, new_revision = [uuid4() for _ in range(6)]
    old_revision = await store.revision()
    async with transaction('RAG_DATABASE_URL') as conn:
        await conn.execute("INSERT INTO rag.documents(id,title) VALUES (%s,'snapshot probe')", (doc,))
        for number, version in enumerate((old_version, new_version), 1):
            await conn.execute("""INSERT INTO rag.document_versions
                (id,document_id,version_number,file_sha256,file_path,media_type,status)
                VALUES (%s,%s,%s,%s,'/unused','text/plain','superseded')""",
                (version, doc, number, bytes([number]) * 32))
        await conn.execute("UPDATE rag.document_versions SET status='active' WHERE id=%s", (old_version,))
        await conn.execute('UPDATE rag.documents SET active_version_id=%s WHERE id=%s', (old_version, doc))
        await conn.execute("""INSERT INTO rag.index_revisions
            (id,model,dimensions,document_template,query_template,state)
            VALUES (%s,%s,%s,'title-content-v1','qwen-retrieval-v2','building')""",
            (new_revision, model, dimensions))
        for chunk, version, revision, size in (
            (old_chunk, old_version, old_revision['id'], 1024),
            (new_chunk, new_version, new_revision, dimensions),
        ):
            await conn.execute("""INSERT INTO rag.chunks
                (id,document_id,version_id,ordinal,content,source_locator,search_text)
                VALUES (%s,%s,%s,1,'alpha evidence','{}','alpha evidence')""", (chunk, doc, version))
            await conn.execute('INSERT INTO rag.embeddings VALUES (%s,%s,%s::vector)',
                               (chunk, revision, vector_literal([0.1] * size)))
    return dict(doc=doc, old_version=old_version, new_version=new_version,
                old_chunk=old_chunk, new_chunk=new_chunk, old_revision=old_revision, new_revision=new_revision)


async def _switch(ids):
    async with transaction('RAG_DATABASE_URL') as conn:
        await conn.execute("UPDATE rag.document_versions SET status='superseded' WHERE id=%s", (ids['old_version'],))
        await conn.execute("UPDATE rag.document_versions SET status='active' WHERE id=%s", (ids['new_version'],))
        await conn.execute('UPDATE rag.documents SET active_version_id=%s WHERE id=%s', (ids['new_version'], ids['doc']))
        await conn.execute("UPDATE rag.index_revisions SET state='superseded' WHERE id=%s", (ids['old_revision']['id'],))
        await conn.execute("UPDATE rag.index_revisions SET state='active' WHERE id=%s", (ids['new_revision'],))
        await conn.execute('UPDATE rag.index_state SET active_revision_id=%s', (ids['new_revision'],))
        # REPEATABLE READ must still see these tuples even after their deletion.
        await conn.execute('DELETE FROM rag.embeddings WHERE revision_id=%s', (ids['old_revision']['id'],))


@pytest.mark.parametrize('pause_after', ['revision', 'dense'])
@pytest.mark.parametrize('change', ['switch', 'delete'])
async def test_recall_snapshot_survives_concurrent_publication_or_deletion(monkeypatch, pause_after, change):
    with using(runtimes.get()['rag']):
        ids = await _seed()
        paused, resume = asyncio.Event(), asyncio.Event()
        connections = []
        original_revision, original_dense, original_sparse = store._revision, store._dense, store._sparse

        async def observe(conn):
            row = await (await conn.execute("""SELECT pg_backend_pid() AS pid,
                current_setting('transaction_isolation') AS isolation,
                current_setting('transaction_read_only') AS readonly""")).fetchone()
            assert row['isolation'] == 'repeatable read' and row['readonly'] == 'on'
            connections.append((conn, row['pid']))

        async def revision(conn):
            await observe(conn)
            result = await original_revision(conn)
            if pause_after == 'revision':
                paused.set()
                await resume.wait()
            return result

        async def dense(conn, revision, vector):
            await observe(conn)
            result = await original_dense(conn, revision, vector)
            if pause_after == 'dense':
                paused.set()
                await resume.wait()
            return result

        async def sparse(conn, terms):
            await observe(conn)
            return await original_sparse(conn, terms)

        monkeypatch.setattr(store, '_revision', revision)
        monkeypatch.setattr(store, '_dense', dense)
        monkeypatch.setattr(store, '_sparse', sparse)
        task = asyncio.create_task(store.recall(ids['old_revision'], [0.1] * 1024, 'alpha'))
        try:
            async with asyncio.timeout(5):
                await paused.wait()
                if change == 'switch':
                    await _switch(ids)
                else:
                    async with transaction('RAG_DATABASE_URL') as writer:
                        assert writer is not connections[0][0]
                        await writer.execute('UPDATE rag.documents SET deleted_at=clock_timestamp() WHERE id=%s', (ids['doc'],))
                resume.set()
                snapshot = await task
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(connections) == 3
        assert all(conn is connections[0][0] and pid == connections[0][1] for conn, pid in connections)
        assert snapshot.revision == ids['old_revision']
        assert [row['id'] for row in snapshot.dense] == [ids['old_chunk']]
        assert [row['id'] for row in snapshot.sparse] == [ids['old_chunk']]
        assert {row['version_id'] for row in snapshot.dense + snapshot.sparse} == {ids['old_version']}

        monkeypatch.setattr(store, '_revision', original_revision)
        monkeypatch.setattr(store, '_dense', original_dense)
        monkeypatch.setattr(store, '_sparse', original_sparse)
        following = await store.recall(ids['old_revision'], [0.1] * 1024, 'alpha')
        if change == 'switch':
            assert following.revision['id'] == ids['new_revision']
            assert [row['id'] for row in following.dense] == [ids['new_chunk']]
            assert [row['id'] for row in following.sparse] == [ids['new_chunk']]
        else:
            assert following.dense == following.sparse == []


@pytest.mark.parametrize('failed_branch', ['dense', 'sparse'])
async def test_recall_sql_timeout_rolls_back_savepoint_and_preserves_other_branch(monkeypatch, failed_branch):
    with using(runtimes.get()['rag']):
        ids = await _seed()
        async def timeout(conn, *args):
            # A real SQL failure leaves the transaction aborted until rollback.
            await conn.execute("SET LOCAL statement_timeout='10ms'")
            await conn.execute('SELECT pg_sleep(1)')
            pytest.fail('query should time out')
        monkeypatch.setattr(store, '_' + failed_branch, timeout)
        result = await store.recall(ids['old_revision'], [0.1] * 1024, 'alpha')
        assert getattr(result, failed_branch) is None
        surviving = result.sparse if failed_branch == 'dense' else result.dense
        assert [row['id'] for row in surviving] == [ids['old_chunk']]


@pytest.mark.parametrize('change', ['same_config', 'model', 'dimensions'])
async def test_search_preparation_switch_uses_snapshot_revision_and_rejects_stale_vector(monkeypatch, change):
    with using(runtimes.get()['rag']):
        ids = await _seed(model='different-model' if change == 'model' else 'Qwen3-Embedding-0.6B',
                          dimensions=512 if change == 'dimensions' else 1024)
        runtime = resources()
        dense_revisions = []
        original_dense, original_audit = store._dense, retrieval._audit

        def outside_transaction():
            assert not any('db-active' in stages for stages in runtime.active.values())

        async def embed(texts, model, dimensions):
            outside_transaction()
            assert model == ids['old_revision']['model'] and dimensions == 1024
            await _switch(ids)
            return [[0.1] * dimensions]

        async def dense(conn, revision, vector):
            dense_revisions.append(revision['id'])
            return await original_dense(conn, revision, vector)

        async def rerank(query, contents):
            outside_transaction()
            return [{'index': i, 'score': 1.0} for i in range(len(contents))]

        async def audit(*args, **kwargs):
            outside_transaction()
            return await original_audit(*args, **kwargs)

        monkeypatch.setattr(retrieval, 'rewrite', lambda query: query)
        monkeypatch.setattr(retrieval, 'embed', embed)
        monkeypatch.setattr(retrieval, 'rerank', rerank)
        monkeypatch.setattr(retrieval, '_audit', audit)
        monkeypatch.setattr(store, '_dense', dense)
        monkeypatch.setattr(runtime.cpu, 'run', lambda *args, **kwargs: 'alpha')
        result = await retrieval.search(retrieval.EvidenceSearchRequest(query='alpha'), 'run', 'tool')
        assert [row['document_version_id'] for row in result['evidences']] == ['ver_' + str(ids['new_version'])]
        assert dense_revisions == ([ids['new_revision']] if change == 'same_config' else [])
        assert result['degradations'] == ([] if change == 'same_config' else ['DENSE_UNAVAILABLE'])
        assert result['status'] == ('ok' if change == 'same_config' else 'degraded')
        async with transaction('RAG_DATABASE_URL', readonly=True) as conn:
            row = await (await conn.execute('SELECT revision_id,candidate_trace FROM rag.retrieval_audit WHERE retrieval_id=%s',
                                           (result['retrieval_id'],))).fetchone()
        assert row['revision_id'] == ids['new_revision']
        assert row['candidate_trace']['stages']['fts']['status'] == 'success'


async def test_recall_cancel_propagates_and_releases_connection(monkeypatch):
    with using(runtimes.get()['rag']):
        revision = await store.revision()
        started = asyncio.Event()
        async def blocked(conn, *args):
            started.set()
            await conn.execute('SELECT pg_sleep(30)')
        monkeypatch.setattr(store, '_dense', blocked)
        task = asyncio.create_task(store.recall(revision, [0.1] * 1024, 'alpha'))
        async with asyncio.timeout(5):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with transaction('RAG_DATABASE_URL') as conn:
                assert (await (await conn.execute('SELECT 1 AS alive')).fetchone())['alive'] == 1
        assert resources().active == {}
