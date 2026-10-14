"""Async document consumer using the reusable CPU pool."""
import asyncio
import logging
from execution.worker import run
from rag_service.computation import sparse_terms,vector_literal
from rag_service.processing import process
from rag_service.storage.jobs import claim,fail,task_ref,LEASE_SECONDS


def main():
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run('rag'))


if __name__=='__main__':raise SystemExit(main())
