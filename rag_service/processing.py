"""Async document pipeline using shared CPU and HTTP resources."""
import asyncio
import os
from execution.persistence import resources, deadline
from execution.scope import authorize, work_owner
from rag_service.computation import prepare_document
from rag_service.models import embed, ModelFailure
from rag_service.parse import ParseFailure
from rag_service.storage import jobs


async def process(job):
    row,revisions=await jobs.inputs(job)
    if not row or not revisions:raise ParseFailure('INDEX_CONFIGURATION_INVALID')
    await authorize()
    drafts,warnings=await resources().cpu.run(prepare_document,row['file_path'],row['media_type'],row['title'],int(os.environ.get('RAG_MAX_CHUNKS','2000')),owner=work_owner(),deadline=deadline(float(os.environ.get('RAG_PARSE_TIMEOUT_SECONDS', '900'))),before_submit=authorize)
    prepared=[]
    for revision in revisions:
        batches=[drafts[start:start+16] for start in range(0,len(drafts),16)]
        vectors=[None]*len(drafts)
        # Four consumers keep capacity bounded without waiting for wave barriers.
        indices = iter(range(len(batches)))
        async def consume():
            for index in indices:
                result = await embed([d['embedding_input'] for d in batches[index]], revision['model'], revision['dimensions'])
                if len(result) != len(batches[index]):
                    raise ModelFailure('EMBEDDING_RESPONSE_INVALID', False)
                vectors[index*16:index*16+len(result)] = result
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(4, len(batches))):
                    group.create_task(consume())
        except BaseExceptionGroup as exc:
            from execution.errors import select_failure
            raise select_failure(exc) from exc
        prepared.append((revision,vectors))
    await jobs.publish(job,row,drafts,warnings,prepared)
