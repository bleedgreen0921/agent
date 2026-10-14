"""Public facade for asynchronous runs storage."""
from agent_service.storage.runs import *

from execution.stores import stores

async def create_run(*args,**kwargs):
    return await stores()[1].create_run(*args,**kwargs)

async def get_run(*args,**kwargs):
    return await stores()[1].get_run(*args,**kwargs)

async def load_result(*args,**kwargs):
    return await stores()[1].load_result(*args,**kwargs)

async def cancel_run(*args,**kwargs):
    return await stores()[1].cancel_run(*args,**kwargs)

async def trace(*args,**kwargs):
    return await stores()[1].trace(*args,**kwargs)

async def timeline(*args,**kwargs):
    return await stores()[1].timeline(*args,**kwargs)
