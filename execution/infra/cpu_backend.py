"""The only CPython-private pool adapter: CPython 3.12 on Linux, spawn only.

Keep process/manager references before shutdown drops them. Never signal a group
unless it belongs to a captured child and its group ID equals that child's PID.
"""
import asyncio
import importlib
import multiprocessing
import os
import pickle
import signal
import sys
from concurrent.futures import ProcessPoolExecutor

from execution.infra.types import ResourceUnavailable


def initialize_process():
    os.setsid()
    # CPU functions may load already provisioned artifacts, never HF metadata.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'



class CpuBackend:
    def __init__(self, workers):
        if sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 12) or sys.platform != 'linux':
            raise ResourceUnavailable('CPU backend requires CPython 3.12 on Linux')
        self.pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
                                        initializer=initialize_process)
        self.processes = []
        self.manager = None
        self.groups = set()

    @staticmethod
    def validate(fn, args, kwargs):
        if not callable(fn) or not hasattr(fn, '__module__') or fn.__module__ == '__main__' or '.' in fn.__qualname__:
            raise ValueError('CPU work requires an importable top-level function')
        if getattr(importlib.import_module(fn.__module__), fn.__name__, None) is not fn:
            raise ValueError('CPU function is not importable by name')
        pickle.dumps((fn, args, kwargs))

    def submit(self, fn, *args, **kwargs):
        try:
            return self.pool.submit(fn, *args, **kwargs)
        finally:
            self.capture()

    def capture(self):
        for process in tuple((self.pool._processes or {}).values()):
            if process not in self.processes:
                self.processes.append(process)
        self.manager = self.pool._executor_manager_thread or self.manager

    def signal(self, sig):
        self.capture()
        for process in self.processes:
            if process.is_alive():
                try:
                    if os.getpgid(process.pid) == process.pid:
                        self.groups.add(process.pid)
                    else:
                        os.kill(process.pid, sig)
                except ProcessLookupError:
                    pass
        # Remember verified group ownership through the retirement. A leader can
        # exit on TERM while one of its descendants ignores TERM.
        for group in self.groups:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass

    async def retire(self, deadline, force=True):
        self.capture()
        self.pool.shutdown(wait=False, cancel_futures=True)
        if force:
            self.signal(signal.SIGTERM)
        kill_at = asyncio.get_running_loop().time() + min(.5, deadline.remaining() / 2)
        while any(p.is_alive() for p in self.processes) or (self.manager and self.manager.is_alive()):
            if deadline.remaining() <= 0:
                self.signal(signal.SIGKILL)
                raise TimeoutError('CPU pool retirement deadline expired')
            if asyncio.get_running_loop().time() >= kill_at:
                self.signal(signal.SIGKILL)
            await asyncio.sleep(.01)
        if force:
            self.signal(signal.SIGKILL)
        for process in self.processes:
            process.join(timeout=0)
        if self.manager:
            self.manager.join(timeout=0)
        # The captured manager owns call/result queues and their named locks.
        # Drop that final owner while the interpreter is alive: API signal
        # propagation may terminate the process before multiprocessing atexit.
        self.manager = None

    @property
    def retired(self):
        return not any(p.is_alive() for p in self.processes) and not (self.manager and self.manager.is_alive())

    def emergency_kill(self):
        self.signal(signal.SIGKILL)
