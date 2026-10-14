"""Test-only native async harness, including owner SQL probes used by older fixtures."""
import asyncio
import inspect
from contextvars import ContextVar
from functools import wraps
from contextlib import asynccontextmanager
import psycopg
from execution.persistence import bind
from execution.infra.runtime import RuntimeResources

runtimes=ContextVar('test_runtimes',default=None)


class AsyncProbeCursor:
    def __init__(self,cursor):self.cursor=cursor
    async def fetchone(self):return self.cursor.fetchone()
    async def fetchall(self):return self.cursor.fetchall()


class AsyncOwnerProbe:
    # This is confined to tests. Production never adapts sync database handles.
    def __init__(self,conn):self.conn=conn
    async def execute(self,*args,**kwargs):return AsyncProbeCursor(self.conn.execute(*args,**kwargs))


async def native(fn,*args,**kwargs):
    args=tuple(AsyncOwnerProbe(a) if isinstance(a,psycopg.Connection) else a for a in args)
    module=getattr(fn,'__module__','')
    role='rag' if module.startswith('rag_service') else 'agent'
    if getattr(fn,'__name__','') in {'beat','snapshot','remove'} and args and args[0]=='rag':role='rag'
    if any(getattr(a,'kind',None)=='rag_document' for a in args):role='rag'
    from agent_service.tooling import budgeted_tool
    if fn==budgeted_tool.awrap_tool_call:
        from tests.acceptance_fixtures import prepare_tool
        if available:=runtimes.get():
            from execution.persistence import using
            with using(available['agent']):args=(await prepare_tool(args[0]),*args[1:])
    if getattr(fn,'__name__','').startswith('awrap_') and args and callable(args[-1]):
        args=(*args[:-1],async_stub(args[-1]))
    available=runtimes.get()
    if available:
        from execution.persistence import _resources
        key=_resources.set(available[role])
        try:return await fn(*args,**kwargs)
        finally:_resources.reset(key)
    return await fn(*args,**kwargs)


def native_runnable(fn,*args,**kwargs):
    from langchain_core.runnables import RunnableLambda
    if inspect.iscoroutinefunction(fn):return RunnableLambda(fn,*args,**kwargs)
    async def run(value):return fn(value)
    return RunnableLambda(run,*args,**kwargs)


def async_stub(fn):
    if inspect.iscoroutinefunction(fn):return fn
    @wraps(fn)
    async def invoke(*args,**kwargs):return fn(*args,**kwargs)
    return invoke


async def run_test(fn,arguments,monkeypatch=None):
    async with RuntimeResources('agent-worker') as agent,RuntimeResources('rag-worker') as rag:
        token=runtimes.set({'agent':agent,'rag':rag})
        try:
            with bind(agent):return await fn(**arguments)
        finally:runtimes.reset(token)


def native_model(cls):
    """Explicit registration of scripted async providers, confined to tests."""
    from agent_service.model_adapter import register_async_model
    from execution.calls import ProviderReply
    async def structured(model, schema, messages):
        from tests.quality_stubs import QUALITY_SCHEMAS,quality_value,payload
        if schema.__name__ in QUALITY_SCHEMAS:
            data=payload(messages)
            value=quality_value(schema.__name__,data)
            custom=getattr(model,'quality_response',None)
            if custom: value=custom(schema.__name__,data,value)
            return ProviderReply(schema.model_validate(value))
        value=await model.with_structured_output(schema).ainvoke(messages)
        if schema.__name__=='Plan':
            for step in value.steps:
                if not step.requirement_ids: step.requirement_ids=['r1']
        return ProviderReply(value)
    register_async_model(cls, structured)
    from langchain_core.language_models import BaseChatModel
    if issubclass(cls, BaseChatModel):
        async def generate(self, *args, **kwargs):
            return self._generate(*args, **kwargs)
        cls._agenerate = generate
    return cls
