"""Public facade for asynchronous runtime storage."""
from agent_service.storage.runtime import *

from execution.stores import stores

async def claim_run(*args,**kwargs):
    return await stores()[1].claim_run(*args,**kwargs)

async def safe_to_resume(*args,**kwargs):
    return await stores()[1].safe_to_resume(*args,**kwargs)

async def interrupt_calls(*args,**kwargs):
    return await stores()[1].interrupt_calls(*args,**kwargs)

async def heartbeat(*args,**kwargs):
    return await stores()[1].heartbeat(*args,**kwargs)

async def control_state(*args,**kwargs):
    return await stores()[1].control_state(*args,**kwargs)

async def revoke_and_finish(*args,**kwargs):
    return await stores()[1].revoke_and_finish(*args,**kwargs)

async def attempt_exited(*args,**kwargs):
    return await stores()[1].attempt_exited(*args,**kwargs)

async def reserve_model(*args,**kwargs):
    return await stores()[1].reserve_model(*args,**kwargs)

async def reserve_tool(*args,**kwargs):
    return await stores()[1].reserve_tool(*args,**kwargs)

async def settle_model(*args,**kwargs):
    return await stores()[1].settle_model(*args,**kwargs)

async def settle_tool(*args,**kwargs):
    return await stores()[1].settle_tool(*args,**kwargs)

async def fail_run(*args,**kwargs):
    return await stores()[1].fail_run(*args,**kwargs)

async def publish_result(*args,**kwargs):
    return await stores()[1].publish_result(*args,**kwargs)
