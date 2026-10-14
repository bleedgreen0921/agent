"""Public facade for asynchronous conversations storage."""
from agent_service.storage.conversations import *

from execution.stores import stores

async def existing(*args,**kwargs):
    return await stores()[1].existing(*args,**kwargs)

async def create_conversation(*args,**kwargs):
    return await stores()[1].create_conversation(*args,**kwargs)

async def list_conversations(*args,**kwargs):
    return await stores()[1].list_conversations(*args,**kwargs)

async def read_conversation(*args,**kwargs):
    return await stores()[1].read_conversation(*args,**kwargs)

async def submit_turn(*args,**kwargs):
    return await stores()[1].submit_turn(*args,**kwargs)

async def existing_turn(*args,**kwargs):
    return await stores()[1].existing_turn(*args,**kwargs)

async def read_turn(*args,**kwargs):
    return await stores()[1].read_turn(*args,**kwargs)

async def cancel_turn(*args,**kwargs):
    return await stores()[1].cancel_turn(*args,**kwargs)

async def list_memories(*args,**kwargs):
    return await stores()[1].list_memories(*args,**kwargs)
