from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from execution.persistence import transaction as connect
from execution import store
from execution.contracts import TaskRef, TaskKind
from execution.inputs import capture
from execution.retry import retry_transaction
CONTEXT_CHARS = 12000
HISTORY_CHARS = 8000
SNAPSHOT_TURNS = 32
RECENT_TURNS = 4

async def _history(conn, conversation_id: UUID, before: int):
    return await (await conn.execute('SELECT t.id,t.ordinal,t.user_text,t.created_at,r.status,rr.answer\n        FROM agent.conversation_turns t JOIN agent.run_state r ON r.id=t.run_id\n        LEFT JOIN agent.run_results rr ON rr.run_id=r.id\n        WHERE t.conversation_id=%s AND t.ordinal<%s ORDER BY t.ordinal', (conversation_id, before))).fetchall()

async def _snapshot_history(conn, conversation_id: UUID, before: int, covered: int):
    lower = min(covered, before - RECENT_TURNS - 1)
    field_chars = 2 * HISTORY_CHARS
    head = (field_chars - 1) // 2
    tail = field_chars - 1 - head
    rows = await (await conn.execute("SELECT t.id,t.ordinal,t.created_at,r.status,\n        CASE WHEN char_length(t.user_text)>%s\n            THEN left(t.user_text,%s) || '…' || right(t.user_text,%s)\n            ELSE t.user_text END AS user_text,\n        CASE WHEN char_length(rr.answer)>%s\n            THEN left(rr.answer,%s) || '…' || right(rr.answer,%s)\n            ELSE rr.answer END AS answer\n        FROM (SELECT id,ordinal,user_text,created_at,run_id FROM agent.conversation_turns\n            WHERE conversation_id=%s AND ordinal>%s AND ordinal<%s\n            ORDER BY ordinal DESC LIMIT %s) t\n        JOIN agent.run_state r ON r.id=t.run_id\n        LEFT JOIN agent.run_results rr ON rr.run_id=r.id\n        ORDER BY t.ordinal DESC", (field_chars, head, tail, field_chars, head, tail, conversation_id, lower, before, SNAPSHOT_TURNS))).fetchall()
    return list(reversed(rows))

@retry_transaction
async def snapshot_base(run_id, token):
    async with connect('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn, TaskRef(run_id, TaskKind.AGENT), token)
        existing = await (await conn.execute('SELECT snapshot FROM agent.run_memory_snapshots WHERE run_id=%s', (run_id,))).fetchone()
        if existing:
            return ('existing', existing['snapshot'])
        turn = await (await conn.execute('SELECT t.conversation_id,t.ordinal\n            FROM agent.conversation_turns t JOIN agent.conversations c ON c.id=t.conversation_id\n            WHERE t.run_id=%s', (run_id,))).fetchone()
        if not turn:
            return ('existing', {})
        summary = await (await conn.execute('SELECT id,version,through_ordinal,content FROM agent.conversation_summaries\n            WHERE conversation_id=%s AND through_ordinal<%s ORDER BY through_ordinal DESC LIMIT 1', (turn['conversation_id'], turn['ordinal']))).fetchone()
        covered = summary['through_ordinal'] if summary else 0
        prior = [_turn_item(r) for r in await _snapshot_history(conn, turn['conversation_id'], turn['ordinal'], covered)]
    return ('new', (turn, summary, covered, prior))

@retry_transaction
async def snapshot_publish(run_id, token, snapshot):
    async with connect('AGENT_DATABASE_URL') as conn:
        return await capture(conn, TaskRef(run_id, TaskKind.AGENT), token, 'run_memory_snapshots', snapshot)

@retry_transaction
async def fact_context(name, vector):
    async with connect('AGENT_DATABASE_URL') as conn:
        hits = await (await conn.execute('SELECT id,statement,source_quote,subject,stated_at,source_turn_id,subject_turn_id\n                FROM agent.personal_facts WHERE embedding IS NOT NULL AND embedding_model=%s AND vector_dims(embedding)=%s\n                ORDER BY embedding <=> %s::vector,id LIMIT 5', (name, len(vector), vector_literal(vector)))).fetchall()
        selected = {r['id']: r for r in hits}
        subjects = list({r['subject'] for r in hits if r['subject']})
        if subjects:
            related = await (await conn.execute('SELECT id,statement,source_quote,subject,stated_at,source_turn_id,subject_turn_id\n                    FROM agent.personal_facts WHERE subject=ANY(%s)\n                    ORDER BY stated_at DESC,id DESC LIMIT 30', (subjects,))).fetchall()
            selected.update({r['id']: r for r in related})
        budget = CONTEXT_CHARS - HISTORY_CHARS
        priority = [*hits, *sorted((r for r in selected.values() if r['id'] not in {h['id'] for h in hits}), key=lambda item: (item['stated_at'], str(item['id'])), reverse=True)]
        bounded = []
        for r in priority:
            cost = len(r['statement']) + len(r['source_quote']) + len(r['subject']) + 150
            if cost <= budget:
                bounded.append(r)
                budget -= cost
        facts = [{**dict(r), 'id': str(r['id']), 'source_turn_id': str(r['source_turn_id']), 'subject_turn_id': str(r['subject_turn_id']), 'stated_at': r['stated_at'].isoformat()} for r in sorted(bounded, key=lambda item: (item['stated_at'], str(item['id'])))]
    return facts

@retry_transaction(profile='control')
async def claim_job(worker_id=None) -> dict | None:
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        row = await store.claim(conn, TaskKind.MEMORY, lease_seconds=300, worker_id=worker_id)
        if not row:
            return None
        business = await (await conn.execute('SELECT kind,turn_id FROM agent.memory_jobs WHERE id=%s', (row['id'],))).fetchone()
        await store.assert_active(conn, job_ref(row), row['lease_token'])
        return {**dict(business), 'id': row['id'], 'attempts': row['attempts'], 'lease_token': row['lease_token'], 'deadline_at': row['execution_deadline_at'], 'remaining_seconds': row['remaining_seconds']}

@retry_transaction(profile='control')
async def finish_job(job: dict, error: str | None=None, *, worker_exit=False, retryable=True) -> bool:
    async with connect('AGENT_DATABASE_URL', profile='control') as conn:
        if error is None:
            await store.complete(conn, job_ref(job), job['lease_token'])
            return True
        return (await store.settle_failure(conn, job_ref(job), job['lease_token'], error, retryable=retryable, worker_exit=worker_exit)).accepted

@retry_transaction
async def extract_inputs(job):
    async with connect('AGENT_DATABASE_URL') as conn:
        row = await (await conn.execute('SELECT t.id,t.conversation_id,t.ordinal,t.user_text,t.created_at\n            FROM agent.conversation_turns t JOIN agent.conversations c ON c.id=t.conversation_id WHERE t.id=%s', (job['turn_id'],))).fetchone()
        prior = await _history(conn, row['conversation_id'], row['ordinal'])
        summary = await (await conn.execute('SELECT content FROM agent.conversation_summaries WHERE conversation_id=%s\n            AND through_ordinal<%s ORDER BY through_ordinal DESC LIMIT 1', (row['conversation_id'], row['ordinal']))).fetchone()
    return (row, prior, summary)

@retry_transaction
async def publish_facts(job, row, valid, vectors, name):
    async with connect('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn, job_ref(job), job['lease_token'])
        for i, fact in enumerate(valid):
            await conn.execute('INSERT INTO agent.personal_facts(id,source_turn_id,statement,\n                source_quote,subject,subject_quote,subject_turn_id,stated_at,embedding,embedding_model)\n                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)\n                ON CONFLICT (source_turn_id,statement,source_quote) DO NOTHING', (uuid4(), row['id'], fact['statement'], fact['source_quote'], fact['subject'], fact['subject_quote'], fact['subject_turn_id'], row['created_at'], vector_literal(vectors[i]) if vectors else None, name))
        await store.complete(conn, job_ref(job), job['lease_token'])

@retry_transaction
async def summary_inputs(job):
    async with connect('AGENT_DATABASE_URL') as conn:
        target = await (await conn.execute('SELECT conversation_id,ordinal FROM agent.conversation_turns WHERE id=%s', (job['turn_id'],))).fetchone()
        prior = await (await conn.execute('SELECT version,through_ordinal,content FROM agent.conversation_summaries\n            WHERE conversation_id=%s ORDER BY through_ordinal DESC LIMIT 1', (target['conversation_id'],))).fetchone()
        covered = prior['through_ordinal'] if prior else 0
        if covered >= target['ordinal']:
            await store.complete(conn, job_ref(job), job['lease_token'])
            return
        rows = await (await conn.execute('SELECT t.ordinal,t.user_text,r.status,rr.answer FROM agent.conversation_turns t\n            JOIN agent.run_state r ON r.id=t.run_id LEFT JOIN agent.run_results rr ON rr.run_id=r.id\n            WHERE t.conversation_id=%s AND t.ordinal>%s AND t.ordinal<=%s ORDER BY t.ordinal', (target['conversation_id'], covered, target['ordinal']))).fetchall()
    return (target, prior, covered, rows)

@retry_transaction
async def publish_summary(job, target, content):
    async with connect('AGENT_DATABASE_URL') as conn:
        await store.assert_active(conn, job_ref(job), job['lease_token'])
        await conn.execute('SELECT id FROM agent.conversations WHERE id=%s FOR UPDATE', (target['conversation_id'],))
        current = await (await conn.execute('SELECT max(through_ordinal) AS n,max(version) AS version FROM agent.conversation_summaries WHERE conversation_id=%s', (target['conversation_id'],))).fetchone()
        if (current['n'] or 0) < target['ordinal']:
            await conn.execute('INSERT INTO agent.conversation_summaries(id,conversation_id,version,through_ordinal,content)\n                VALUES (%s,%s,%s,%s,%s)', (uuid4(), target['conversation_id'], (current['version'] or 0) + 1, target['ordinal'], content))
        await store.complete(conn, job_ref(job), job['lease_token'])

def vector_literal(vector: list[float]) -> str:
    return '[' + ','.join((str(float(x)) for x in vector)) + ']'

def _turn_item(r):
    return {'turn_id': str(r['id']), 'ordinal': r['ordinal'], 'user': r['user_text'], 'assistant': r['answer'], 'status': r['status'], 'created_at': r['created_at'].isoformat()}

def job_ref(job):
    return TaskRef(job['id'],TaskKind.MEMORY)
