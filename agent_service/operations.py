"""Public facade for asynchronous operations storage."""
from agent_service.storage.operations import *

from execution.stores import stores

async def list_runs(*args,**kwargs):
    return await stores()[1].list_runs(*args,**kwargs)

async def run_queue_summary(*args,**kwargs):
    return await stores()[1].run_queue_summary(*args,**kwargs)
