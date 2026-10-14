from rag_service.models import embed
"""Public facade for native async reindex storage."""
from rag_service.storage.reindex import *

from execution.stores import stores

async def start(*args,**kwargs):
    return await stores()[3].start(*args,**kwargs)

async def build(revision_id,batch_size=16):
    from rag_service.chunking import ChunkDraft,embedding_input
    completed=0
    while True:
        revision,rows=await stores()[3].next_batch(revision_id,batch_size)
        if not rows:return completed
        drafts=[ChunkDraft(row['content'],tuple(row['heading_path']),{}) for row in rows]
        vectors=await embed([embedding_input(row['title'],draft) for row,draft in zip(rows,drafts)],revision['model'],revision['dimensions'])
        completed+=await stores()[3].publish_batch(revision_id,rows,vectors)

async def switch(*args,**kwargs):
    return await stores()[3].switch(*args,**kwargs)


async def _cli():
    import argparse
    from uuid import UUID
    from execution.infra.runtime import RuntimeResources
    from execution.persistence import bind
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('start','build','switch'))
    parser.add_argument('revision_id',nargs='?',type=UUID)
    parser.add_argument('--model');parser.add_argument('--dimensions',type=int)
    args=parser.parse_args()
    async with RuntimeResources('rag-worker') as runtime:
        with bind(runtime):
            if args.command=='start':print(await start(args.model,args.dimensions))
            elif args.command=='build':print(await build(args.revision_id))
            else:await switch(args.revision_id)

if __name__=='__main__':
    import asyncio
    asyncio.run(_cli())
