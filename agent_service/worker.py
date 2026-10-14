"""Async Agent and Memory consumers; only shared CPU work uses child processes."""
import asyncio
import logging
from execution.worker import run


def main():
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run('agent'))


if __name__=='__main__':raise SystemExit(main())
