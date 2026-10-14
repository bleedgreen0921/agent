"""Receipts attest to real, immutable graph output, never to cached call results."""
from uuid import UUID
from langchain_core.messages import BaseMessage
from execution import store
from execution.contracts import TaskRef, TaskKind

MARKER = '_execution_call'
MARKER_CHANNEL = '_execution_calls'


def marker(kind, call_id):
    return {'kind': kind, 'id': str(call_id)}


def markers(value):
    if isinstance(value, BaseMessage):
        item = value.additional_kwargs.get(MARKER)
        if item:
            if set(item) != {'kind','id'} or item['kind'] not in ('model','tool'):
                raise ValueError('Invalid execution marker')
            yield item['kind'], UUID(item['id'])
    elif isinstance(value, (list,tuple)):
        for item in value: yield from markers(item)
    elif isinstance(value, dict):
        # Structured nodes reserve this field. Do not inspect arbitrary model text.
        for item in value.get(MARKER_CHANNEL, []):
            yield item['kind'], UUID(item['id'])
        for key, item in value.items():
            if key != MARKER_CHANNEL: yield from markers(item)


async def confirm(conn, run_id, token, calls, *, source, thread, ns, checkpoint_id,
                  channel, digest, graph_task_id='', write_idx=-1):
    for kind, call_id in set(calls):
        table = 'model_calls' if kind == 'model' else 'tool_calls'
        call = await (await conn.execute(f'SELECT status,attempt_token,step_id FROM agent.{table} WHERE run_id=%s AND id=%s', (run_id,call_id))).fetchone()
        if not call or call['status'] == 'started':
            raise PermissionError('Checkpoint contains an unconfirmed call')
        expected_thread=str(run_id)
        if call['step_id'] is not None:
            step=await (await conn.execute('SELECT ordinal FROM agent.run_steps WHERE run_id=%s AND id=%s',(run_id,call['step_id']))).fetchone()
            if not step:raise PermissionError('Call step does not belong to Run')
            expected_thread+=':step:'+str(step['ordinal'])
        if thread!=expected_thread:raise PermissionError('Call output belongs to another graph thread')
        column = 'model_call_id' if kind == 'model' else 'tool_call_id'
        params = (run_id,call_id,source,thread,ns,checkpoint_id,graph_task_id,write_idx,channel,digest)
        row = await (await conn.execute(f'''INSERT INTO agent.call_checkpoint_receipts
            (run_id,{column},source,thread_id,checkpoint_ns,checkpoint_id,graph_task_id,write_idx,channel,payload_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING payload_sha256''', params)).fetchone()
        if not row:
            row = await (await conn.execute(f'''SELECT payload_sha256 FROM agent.call_checkpoint_receipts
                WHERE run_id=%s AND {column}=%s AND source=%s AND thread_id=%s AND checkpoint_ns=%s
                  AND checkpoint_id=%s AND graph_task_id=%s AND write_idx=%s AND channel=%s''', params[:-1])).fetchone()
        if not row or bytes(row['payload_sha256']) != bytes(digest):
            raise ValueError('Checkpoint receipt conflict')
        # Old successful calls can appear in a later attempt's carried state.
        # New receipts for them require an already existing receipt.
        if call['attempt_token'] != token:
            old = await (await conn.execute(f'''SELECT 1 FROM agent.call_checkpoint_receipts
                WHERE run_id=%s AND {column}=%s AND NOT
                (source=%s AND thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s
                 AND graph_task_id=%s AND write_idx=%s AND channel=%s) LIMIT 1''', params[:-1])).fetchone()
            if not old:
                raise PermissionError('Old call has no durable recovery proof')
        await conn.execute(f'UPDATE agent.{table} SET checkpointed=true WHERE run_id=%s AND id=%s', (run_id,call_id))


def anchor_query(r):
    thread,ns,cp=r['thread_id'],r['checkpoint_ns'],r['checkpoint_id']
    if r['source']=='pending_write':
        return ("""SELECT sha256(convert_to(w.type,'UTF8')||w.blob) AS digest
            FROM agent.checkpoint_writes w WHERE w.thread_id=%s AND w.checkpoint_ns=%s AND w.checkpoint_id=%s
              AND w.task_id=%s AND w.idx=%s AND w.channel=%s
              AND w.checkpoint_id=(SELECT checkpoint_id FROM agent.checkpoints c WHERE c.thread_id=w.thread_id AND c.checkpoint_ns=w.checkpoint_ns ORDER BY checkpoint_id DESC LIMIT 1)""",
              (thread,ns,cp,r['graph_task_id'],r['write_idx'],r['channel']))
    return ("""WITH RECURSIVE chain AS (
        (SELECT checkpoint_id,parent_checkpoint_id FROM agent.checkpoints WHERE thread_id=%s AND checkpoint_ns=%s ORDER BY checkpoint_id DESC LIMIT 1)
        UNION ALL SELECT c.checkpoint_id,c.parent_checkpoint_id FROM agent.checkpoints c JOIN chain p ON c.checkpoint_id=p.parent_checkpoint_id
        WHERE c.thread_id=%s AND c.checkpoint_ns=%s)
        SELECT sha256(convert_to(c.checkpoint::text,'UTF8')||convert_to(b.type,'UTF8')||b.blob) AS digest
        FROM agent.checkpoints c JOIN chain ON chain.checkpoint_id=c.checkpoint_id
        JOIN agent.checkpoint_blobs b ON b.thread_id=c.thread_id AND b.checkpoint_ns=c.checkpoint_ns
          AND b.channel=%s AND b.version=c.checkpoint->'channel_versions'->>%s
        WHERE c.thread_id=%s AND c.checkpoint_ns=%s AND c.checkpoint_id=%s""",
        (thread,ns,thread,ns,r['channel'],r['channel'],thread,ns,cp))


def digest_matches(row,receipt):
    return bool(row and bytes(row['digest'])==bytes(receipt['payload_sha256']))


async def anchor_valid(conn,r):
    query,params=anchor_query(r)
    return digest_matches(await (await conn.execute(query,params)).fetchone(),r)


async def resumable(conn, run_id):
    run = await (await conn.execute('SELECT recovery_protocol FROM agent.agent_runs WHERE id=%s',(run_id,))).fetchone()
    if not run or run['recovery_protocol'] != 2: return False
    calls = await (await conn.execute('''SELECT 'model' AS kind,id,status FROM agent.model_calls WHERE run_id=%s
        UNION ALL SELECT 'tool',id,status FROM agent.tool_calls WHERE run_id=%s''',(run_id,run_id))).fetchall()
    receipts = await (await conn.execute('SELECT * FROM agent.call_checkpoint_receipts WHERE run_id=%s',(run_id,))).fetchall()
    root = str(run_id)
    for call in calls:
        if call['status'] == 'started': return False
        valid = False
        for r in receipts:
            if (r['model_call_id'] if call['kind']=='model' else r['tool_call_id']) != call['id']: continue
            if r['thread_id'] != root:
                if not r['thread_id'].startswith(root+':step:'): return False
                try: ordinal = int(r['thread_id'][len(root+':step:'):])
                except ValueError: return False
                if not await (await conn.execute('SELECT 1 FROM agent.run_steps WHERE run_id=%s AND ordinal=%s',(run_id,ordinal))).fetchone(): return False
            if await anchor_valid(conn,r): valid = True; break
        if not valid: return False
    return True




def resumable_offline(conn,run_id):
    """Owner diagnostic only; shares the exact anchor SQL and digest rules."""
    run=conn.execute('SELECT recovery_protocol FROM agent.agent_runs WHERE id=%s',(run_id,)).fetchone()
    if not run or run['recovery_protocol']!=2:return False
    calls=conn.execute("SELECT 'model' AS kind,id,status FROM agent.model_calls WHERE run_id=%s UNION ALL SELECT 'tool',id,status FROM agent.tool_calls WHERE run_id=%s",(run_id,run_id)).fetchall()
    receipts=conn.execute('SELECT * FROM agent.call_checkpoint_receipts WHERE run_id=%s',(run_id,)).fetchall()
    steps={str(run_id)+':step:'+str(r['ordinal']) for r in conn.execute('SELECT ordinal FROM agent.run_steps WHERE run_id=%s',(run_id,)).fetchall()}
    threads={str(run_id),*steps}
    for call in calls:
        if call['status']=='started':return False
        valid=False
        for receipt in receipts:
            if (receipt['model_call_id'] if call['kind']=='model' else receipt['tool_call_id'])!=call['id']:continue
            if receipt['thread_id'] not in threads:return False
            query,params=anchor_query(receipt)
            if digest_matches(conn.execute(query,params).fetchone(),receipt):valid=True;break
        if not valid:return False
    return True
