"""Initialize LangGraph checkpoint tables explicitly; API/Worker never migrate."""

import os

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db.connection import bounded_conninfo


def with_agent_search_path(dsn: str, *, runtime: bool = False) -> str:
    if runtime:
        dsn, timeout = bounded_conninfo(dsn)
    else:
        timeout = None
    values = conninfo_to_dict(dsn)
    existing = values.get("options", "")
    values["options"] = (existing + " -c search_path=agent").strip()
    if timeout is not None:
        values["connect_timeout"] = str(timeout)
    return make_conninfo(**values)


def main() -> None:
    import psycopg
    from db.migrate import validate_database
    with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"]) as conn:
        validate_database(conn)
    with PostgresSaver.from_conn_string(with_agent_search_path(os.environ["MIGRATION_DATABASE_URL"])) as saver:
        saver.setup()


if __name__ == "__main__":
    main()


from contextlib import asynccontextmanager
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.base import CheckpointTuple, WRITES_IDX_MAP, get_serializable_checkpoint_metadata
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from execution import store
from execution.persistence import resources, transaction, deadline
from execution.infra.types import WorkOwner
from agent_service.storage.receipts import markers, confirm, MARKER_CHANNEL
from execution.retry import retry_transaction


class ProtectedSaver(AsyncPostgresSaver):
    """One pool borrow per operation; graph SQL and receipts commit together."""
    def __init__(self, task, token):
        super().__init__(resources().db.pools['normal'])
        self.task, self.token = task, token
        self.owner = WorkOwner(task, token)


    async def _validate_thread(self, config):
        from execution.scope import track_call_task
        track_call_task()
        thread = config['configurable']['thread_id']
        root = str(self.task.id)
        if thread == root: return
        prefix = root + ':step:'
        if not thread.startswith(prefix): raise PermissionError('Checkpoint thread belongs to another task')
        try: ordinal = int(thread[len(prefix):])
        except ValueError: raise PermissionError('Invalid step thread') from None
        if not 1 <= ordinal <= 5 or thread != prefix + str(ordinal): raise PermissionError('Invalid step thread')
        async with transaction('AGENT_DATABASE_URL', readonly=True) as conn:
            if not await (await conn.execute('SELECT 1 FROM agent.run_steps WHERE run_id=%s AND ordinal=%s',(self.task.id,ordinal))).fetchone():
                raise PermissionError('Step does not exist')

    @asynccontextmanager
    async def _cursor(self, *, pipeline=False):
        async with transaction('AGENT_DATABASE_URL') as conn:
            await conn.execute('SET LOCAL search_path=agent')
            await store.assert_active(conn,self.task,self.token)
            async with conn.cursor(binary=True,row_factory=dict_row) as cur:
                yield cur
            await store.assert_active(conn,self.task,self.token)

    async def _data(self, fn, *args):
        return await resources().io.run(fn,*args,owner=self.owner,deadline=deadline())



    @retry_transaction
    async def aput(self,config,checkpoint,metadata,new_versions):
        await self._validate_thread(config)
        cfg=config['configurable']; thread=cfg['thread_id']; ns=cfg.get('checkpoint_ns',''); cp=checkpoint['id']
        copy={**checkpoint,'channel_values':dict(checkpoint['channel_values'])}
        blobs={k:copy['channel_values'].pop(k) for k,v in checkpoint['channel_values'].items()
               if v is not None and (not isinstance(v,(str,int,float,bool)) or isinstance(v,str) and len(v.encode())>=4096)}
        params=await self._data(self._dump_blobs,thread,ns,blobs,{k:v for k,v in new_versions.items() if k in blobs})
        meta=get_serializable_checkpoint_metadata(config,metadata)
        async with transaction('AGENT_DATABASE_URL') as conn:
            await conn.execute('SET LOCAL search_path=agent')
            await store.assert_active(conn,self.task,self.token)
            async with conn.cursor(binary=True,row_factory=dict_row) as cur:
                if params: await cur.executemany(self.UPSERT_CHECKPOINT_BLOBS_SQL,params)
                for blob in params:
                    await cur.execute('SELECT type,blob FROM checkpoint_blobs WHERE thread_id=%s AND checkpoint_ns=%s AND channel=%s AND version=%s',blob[:4])
                    actual=await cur.fetchone()
                    if not actual or actual['type']!=blob[-2] or bytes(actual['blob'] or b'')!=bytes(blob[-1] or b''):
                        raise ValueError('Checkpoint blob conflict')
                await cur.execute(self.UPSERT_CHECKPOINTS_SQL,(thread,ns,cp,cfg.get('checkpoint_id'),Jsonb(copy),Jsonb(meta)))
            for channel,value in blobs.items():
                calls=list(markers({MARKER_CHANNEL:value} if channel==MARKER_CHANNEL else value))
                if not calls: continue
                row=await (await conn.execute("SELECT sha256(convert_to(c.checkpoint::text,'UTF8')||convert_to(b.type,'UTF8')||b.blob) AS digest FROM agent.checkpoints c JOIN agent.checkpoint_blobs b ON b.thread_id=c.thread_id AND b.checkpoint_ns=c.checkpoint_ns AND b.channel=%s AND b.version=c.checkpoint->'channel_versions'->>%s WHERE c.thread_id=%s AND c.checkpoint_ns=%s AND c.checkpoint_id=%s",(channel,channel,thread,ns,cp))).fetchone()
                if not row: raise ValueError('Checkpoint output was not persisted')
                await confirm(conn,self.task.id,self.token,calls,source='checkpoint',thread=thread,ns=ns,checkpoint_id=cp,channel=channel,digest=row['digest'])
            await store.assert_active(conn,self.task,self.token)
        return {'configurable':{'thread_id':thread,'checkpoint_ns':ns,'checkpoint_id':cp}}

    @retry_transaction
    async def aput_writes(self,config,writes,task_id,task_path=''):
        await self._validate_thread(config)
        cfg=config['configurable']; thread=cfg['thread_id']; ns=cfg.get('checkpoint_ns',''); cp=cfg['checkpoint_id']
        params=await self._data(self._dump_writes,thread,ns,cp,task_id,task_path,writes)
        query=self.UPSERT_CHECKPOINT_WRITES_SQL if all(w[0] in WRITES_IDX_MAP for w in writes) else self.INSERT_CHECKPOINT_WRITES_SQL
        async with transaction('AGENT_DATABASE_URL') as conn:
            await conn.execute('SET LOCAL search_path=agent')
            await store.assert_active(conn,self.task,self.token)
            async with conn.cursor(binary=True,row_factory=dict_row) as cur:
                await cur.executemany(query,params)
            for index,(channel,value) in enumerate(writes):
                if channel in WRITES_IDX_MAP: continue
                calls=list(markers({MARKER_CHANNEL:value} if channel==MARKER_CHANNEL else value))
                if not calls: continue
                row=await (await conn.execute("SELECT type,blob,sha256(convert_to(type,'UTF8')||blob) AS digest FROM agent.checkpoint_writes WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s AND task_id=%s AND idx=%s AND channel=%s",(thread,ns,cp,task_id,index,channel))).fetchone()
                submitted=params[index]
                if not row or row['type']!=submitted[-2] or bytes(row['blob'])!=bytes(submitted[-1]):
                    raise ValueError('Pending write conflict')
                await confirm(conn,self.task.id,self.token,calls,source='pending_write',thread=thread,ns=ns,checkpoint_id=cp,channel=channel,digest=row['digest'],graph_task_id=task_id,write_idx=index)
            await store.assert_active(conn,self.task,self.token)

    def _decode(self,value):
        if value['checkpoint']['v']<4: raise ValueError('Historical checkpoint execution is unsupported')
        return CheckpointTuple({'configurable':{'thread_id':value['thread_id'],'checkpoint_ns':value['checkpoint_ns'],'checkpoint_id':value['checkpoint_id']}},
            {**value['checkpoint'],'channel_values':{**(value['checkpoint'].get('channel_values') or {}),**self._load_blobs(value['channel_values'])}},
            value['metadata'],{'configurable':{'thread_id':value['thread_id'],'checkpoint_ns':value['checkpoint_ns'],'checkpoint_id':value['parent_checkpoint_id']}} if value['parent_checkpoint_id'] else None,
            self._load_writes(value['pending_writes']))

    @retry_transaction
    async def aget_tuple(self,config):
        await self._validate_thread(config)
        cfg=config['configurable']; args=[cfg['thread_id'],cfg.get('checkpoint_ns','')]
        query=self.SELECT_SQL+' WHERE thread_id=%s AND checkpoint_ns=%s'
        if cfg.get('checkpoint_id'): query+=' AND checkpoint_id=%s'; args.append(cfg['checkpoint_id'])
        else: query+=' ORDER BY checkpoint_id DESC LIMIT 1'
        async with self._cursor() as cur:
            await cur.execute(query,args,binary=True); value=await cur.fetchone()
        return await self._data(self._decode,value) if value else None

    async def alist(self,config,*,filter=None,before=None,limit=None):
        if config is None: raise PermissionError('A bound thread is required')
        await self._validate_thread(config)
        if before: await self._validate_thread(before)
        where,args=self._search_where(config,filter,before)
        query=self.SELECT_SQL+where+' ORDER BY checkpoint_id DESC'
        if limit is not None: query+=' LIMIT %s'; args=[*args,int(limit)]
        async with self._cursor() as cur:
            await cur.execute(query,args,binary=True); values=await cur.fetchall()
        for value in values: yield await self._data(self._decode,value)

    async def adelete_thread(self,thread_id):
        raise PermissionError('Runtime checkpoint deletion is disabled')

    def _sync_forbidden(self,*args,**kwargs):
        raise RuntimeError('ProtectedSaver requires its native async interface')
    put=put_writes=get_tuple=list=delete_thread=_sync_forbidden


@asynccontextmanager
async def protected_saver(_dsn,task,token):
    yield ProtectedSaver(task,token)
