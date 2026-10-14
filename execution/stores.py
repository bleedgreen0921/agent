"""Injected storage interfaces; transaction primitives remain explicit in execution.store."""
from functools import wraps
from contextlib import contextmanager
from execution.persistence import using, resources, transaction
from execution.contracts import TaskRef
from execution.retry import retry_transaction


class TaskStore:
    def __init__(self, runtime):self.runtime=runtime

    @retry_transaction
    async def create(self,task:TaskRef,*,queue_seconds=None):
        from execution import store
        with using(self.runtime):
            async with transaction() as conn:return await store.create(conn,task,queue_seconds=queue_seconds)

    async def claim(self,kind,worker_id=None):
        from execution.contracts import TaskKind
        index={TaskKind.AGENT:1,TaskKind.MEMORY:2,TaskKind.DOCUMENT:3}[kind]
        method='claim_run' if kind==TaskKind.AGENT else 'claim_job' if kind==TaskKind.MEMORY else 'claim'
        return await stores(self.runtime)[index].call(method,worker_id)

    async def observe(self,task,token):
        from execution import store
        with using(self.runtime):return await store.check(task,token)

    async def observe_control(self, task: TaskRef):
        from execution import store
        with using(self.runtime):
            return await store.control_snapshot(task)

    async def renew(self,task,token,seconds):
        from execution import store
        with using(self.runtime):return await store.renew(task,token,seconds)

    @retry_transaction(profile='control')
    async def stop(self,task):
        from execution import store
        with using(self.runtime):
            async with transaction(profile='control') as conn:return await store.stop(conn,task)

    @retry_transaction(profile='control')
    async def settle(self,task,token,code,**options):
        from execution import store
        with using(self.runtime):
            async with transaction(profile='control') as conn:return await store.settle_failure(conn,task,token,code,**options)


class BusinessStore:
    modules=()
    def __init__(self,runtime):self.runtime=runtime
    async def call(self,name,*args,**kwargs):
        import importlib
        for module in self.modules:
            fn=getattr(importlib.import_module(module),name,None)
            import inspect
            if inspect.iscoroutinefunction(fn):
                with using(self.runtime):return await fn(*args,**kwargs)
        raise AttributeError(name)

    def __getattr__(self,name):
        if name.startswith('_'):raise AttributeError(name)
        async def invoke(*args,**kwargs):return await self.call(name,*args,**kwargs)
        return invoke


class AgentStore(BusinessStore):
    modules=('agent_service.storage.runtime','agent_service.storage.runs','agent_service.storage.operations','agent_service.storage.conversations','agent_service.storage.graph','agent_service.storage.acceptance')


class MemoryStore(BusinessStore):
    modules=('agent_service.storage.memory',)


class DocumentStore(BusinessStore):
    modules=('rag_service.storage.jobs','rag_service.storage.documents','rag_service.storage.reindex','rag_service.storage.retrieval')


def stores(runtime=None):
    runtime=runtime or resources()
    if not hasattr(runtime,'persistent_stores'):
        runtime.persistent_stores=(TaskStore(runtime),AgentStore(runtime),MemoryStore(runtime),DocumentStore(runtime))
    return runtime.persistent_stores
