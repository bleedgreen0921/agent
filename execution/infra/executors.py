"""Bounded submission; logical cancellation never frees physical capacity."""
import asyncio
import contextvars
import inspect
from time import monotonic

import httpx
from psycopg import AsyncConnection
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass

from execution.infra.cpu_backend import CpuBackend
from execution.infra.types import (CapacityTimeout, CpuPoolReset, OperationDeadline,
                                  ResourceClosed, WorkRecord, WorkStopped)


@dataclass
class Job:
    id: object
    owner: object
    fn: object
    args: tuple
    kwargs: dict
    result: asyncio.Future
    context: object
    deadline: OperationDeadline
    before_submit: object = None
    admission: object = None
    physical: object = None
    timer: object = None
    generation: int = 0
    timing: object = None
    timing_parent: str | None = None
    queued_at: float = 0
    submitted_at: float = 0


class BoundedExecutor:
    def __init__(self, runtime, backend):
        self.runtime, self.backend = runtime, backend
        self.capacity = getattr(runtime.config, backend + '_workers')
        self.pending = getattr(runtime.config, backend + '_pending')
        self.queue = deque()
        self.jobs = {}
        self.changed = asyncio.Event()
        self.pool = None
        self.generation = 0
        self.reset_count = 0
        self.reset_task = None
        self.frozen = False
        self.closing = False
        self.shutdown_called = False

    def wake(self):
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    def _occupied(self):
        return sum((job.physical is not None and not job.physical.done()) or
                   (job.admission is not None and not job.admission.done()) for job in self.jobs.values())

    @staticmethod
    def _validate_io(fn, args, kwargs):
        if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
            raise ValueError('IO work must be a synchronous callable')
        pending = [args, kwargs, getattr(fn, '__self__', None)]
        visited = set()
        while pending:
            value = pending.pop()
            if isinstance(value, (AsyncConnection, httpx.AsyncClient)):
                raise ValueError('Async connections and clients cannot enter the IO executor')
            if id(value) in visited:
                continue
            visited.add(id(value))
            if isinstance(value, dict):
                pending.extend(value.values())
                pending.extend(value.keys())
            elif isinstance(value, (list, tuple, set, frozenset)):
                pending.extend(value)

    async def run(self, fn, *args, owner=None, deadline, before_submit=None, **kwargs):
        from execution.progress import activity
        async with activity(self.backend + ':' + getattr(fn, '__name__', 'work'), deadline):
            return await self._run(fn, *args, owner=owner, deadline=deadline, before_submit=before_submit, **kwargs)

    async def _run(self, fn, *args, owner=None, deadline, before_submit=None, **kwargs):
        self.runtime.check()
        if self.backend == 'io':
            self._validate_io(fn, args, kwargs)
        if owner and owner.attempt in self.runtime.stopped_attempts:
            raise WorkStopped('Execution attempt stopped')
        if self.backend == 'cpu':
            CpuBackend.validate(fn, args, kwargs)
        job = None
        try:
            async with asyncio.timeout_at(deadline.when):
                # Full outer queue: caller itself waits, no helper Task or unbounded job.
                while len(self.queue) >= self.pending:
                    self.runtime.check()
                    if owner and owner.attempt in self.runtime.stopped_attempts:
                        raise WorkStopped('Execution attempt stopped')
                    await self.changed.wait()
                self.runtime.check()
                record = WorkRecord(self.backend, owner)
                work_id = self.runtime.registry.register(record)
                job = Job(work_id, owner, fn, args, kwargs, self.runtime.loop.create_future(),
                          contextvars.copy_context(), deadline, before_submit=before_submit)
                from execution.timing import collector,stage_name
                job.timing=collector.get();job.timing_parent=stage_name.get();job.queued_at=monotonic()
                self.jobs[work_id] = job
                self.queue.append(job)
                self._pump()
                return await asyncio.shield(job.result)
        except TimeoutError as exc:
            if job:
                self._stop(job, 'deadline')
            if job is None or job.physical is None:
                raise CapacityTimeout('Executor admission deadline expired') from exc
            raise
        except asyncio.CancelledError:
            if job:
                self._stop(job, 'cancelled')
            raise
        finally:
            if job and job.result.done() and not job.result.cancelled():
                job.result.exception()  # Consume even when completion races caller cancellation.

    def _pump(self):
        if self.frozen or self.closing or not self.runtime.accepting:
            return
        while self.queue and not self.frozen and not self.closing and self._occupied() < self.capacity:
            job = self.queue.popleft()
            self.wake()
            if job.deadline.remaining() <= 0:
                self.runtime.registry.stop(job.id, 'deadline')
                if not job.result.done():
                    job.result.set_exception(CapacityTimeout('Executor admission deadline expired'))
                self._discard(job)
                continue
            if self.runtime.registry.stopped(job.id):
                self._discard(job)
                continue
            if job.before_submit:
                job.admission = self.runtime.loop.create_task(self._admit(job), context=job.context.copy())
            else:
                self._submit(job)

    async def _admit(self, job):
        try:
            async with asyncio.timeout_at(job.deadline.when):
                await job.before_submit()
            if job.id in self.jobs and not self.runtime.registry.stopped(job.id):
                if job.deadline.remaining() <= 0:
                    raise CapacityTimeout('Executor deadline expired before dispatch')
                self.runtime.check()
                if self.frozen:
                    self.queue.appendleft(job)
                else:
                    self._submit(job)
        except BaseException as exc:
            if job.id in self.jobs:
                if not job.result.done():
                    job.result.set_exception(exc)
                self._discard(job)
        finally:
            job.admission = None
            self._pump()

    def _submit(self, job):
        job.submitted_at=monotonic()
        if job.timing is not None:
            job.timing['spans'].append({'name':self.backend+'_queue_wait','category':'wait','parent':job.timing_parent,
                'offset_ms':round((job.queued_at-job.timing['started'])*1000,3),
                'duration_ms':round((job.submitted_at-job.queued_at)*1000,3),'status':'success'})
        if self.pool is None:
            try:
                self.pool = (ThreadPoolExecutor(max_workers=self.capacity, thread_name_prefix='runtime-io')
                             if self.backend == 'io' else CpuBackend(self.capacity))
            except Exception as exc:
                job.result.set_exception(exc)
                self._discard(job)
                return
        job.generation = self.generation
        if not self.runtime.registry.submit(job.id, self.generation):
            self._discard(job)
            return
        try:
            job.physical = (self.pool.submit(job.context.run, job.fn, *job.args, **job.kwargs)
                            if self.backend == 'io' else self.pool.submit(job.fn, *job.args, **job.kwargs))
        except Exception as exc:
            self.runtime.registry.complete(job.id)
            job.result.set_exception(CpuPoolReset('CPU pool interrupted') if isinstance(exc, BrokenProcessPool) else exc)
            self._discard(job)
            if isinstance(exc, BrokenProcessPool):
                self._begin_reset()
            return
        job.physical.add_done_callback(lambda future, job=job: self._physical_done(job, future))

    def _physical_done(self, job, future):
        # Runs in an executor callback thread, including after loop.close().
        self.runtime.registry.complete(job.id)
        if job.timing is not None:
            job.timing['spans'].append({'name':self.backend+'_execution_wall','category':'execute','parent':job.timing_parent,
                'offset_ms':round((job.submitted_at-job.timing['started'])*1000,3),
                'duration_ms':round((monotonic()-job.submitted_at)*1000,3),'status':'finished'})
        try:
            value, error = future.result(), None
        except BaseException as exc:
            value, error = None, exc
        if isinstance(error, BrokenProcessPool):
            # Freeze delivery/pumping before callbacks for other failed work arrive.
            error = CpuPoolReset('CPU pool interrupted')
        try:
            self.runtime.loop.call_soon_threadsafe(self._deliver, job, value, error)
        except RuntimeError:
            pass  # Physical completion is real; no fake completion for other jobs.

    def _deliver(self, job, value, error):
        if job.id not in self.jobs:
            return
        if isinstance(error, CpuPoolReset):
            self._begin_reset()
        if not job.result.done():
            if self.runtime.registry.stopped(job.id):
                job.result.set_exception(WorkStopped('Work stopped'))
            elif error:
                job.result.set_exception(error)
            else:
                job.result.set_result(value)
        self._discard(job)
        self._pump()

    def _discard(self, job):
        if job.timer:
            job.timer.cancel()
        if job in self.queue:
            self.queue.remove(job)
        self.runtime.registry.complete(job.id)
        self.runtime.registry.release(job.id)
        self.jobs.pop(job.id, None)
        self.wake()

    def _stop(self, job, reason):
        if not self.runtime.registry.stop(job.id, reason):
            return
        # Cancel the waiter even if the physical Future already completed: delivery
        # is still forbidden if cancellation wins on the loop.
        if not job.result.done():
            job.result.cancel()
        if job.physical is None:
            if job.admission is not None and not job.admission.done():
                job.admission.cancel()
            else:
                self._discard(job)
        elif not job.physical.cancel() and not job.physical.done() and job.timer is None:
            job.timer = self.runtime.loop.call_later(self.runtime.config.cleanup_grace, self._overdue, job)

    def _overdue(self, job):
        if job.id not in self.jobs or job.physical.done():
            return
        if self.backend == 'io':
            self.runtime.request_exit('io_cleanup_grace_expired')
        else:
            self._begin_reset()

    def cancel_attempt(self, owner):
        for job in list(self.jobs.values()):
            if job.owner and job.owner.attempt == owner.attempt:
                self._stop(job, 'attempt_stopped')
        self.wake()

    def _begin_reset(self):
        if self.backend != 'cpu' or self.frozen or self.closing:
            return
        self.frozen = True
        # Interrupt all current-generation submitted callers immediately; capacity
        # remains registered until a callback or retirement confirms completion.
        for job in list(self.jobs.values()):
            if job.physical is not None:
                self.runtime.registry.stop(job.id, 'cpu_pool_reset')
                if not job.result.done():
                    job.result.set_exception(CpuPoolReset('CPU pool generation interrupted'))
        self.reset_task = self.runtime.loop.create_task(self._reset())

    async def _reset(self):
        old = self.pool
        try:
            deadline = OperationDeadline.after(self.runtime.config.cpu_reset_timeout)
            if old:
                async with asyncio.timeout_at(deadline.when):
                    await old.retire(deadline)
            for job in list(self.jobs.values()):
                if job.physical is not None:
                    self._discard(job)
            self.pool = None
            self.generation += 1
            self.reset_count += 1
            self.frozen = False
            self._pump()
        except Exception:
            self.runtime.request_exit('cpu_pool_retirement_failed')
        finally:
            self.wake()

    def quiesce(self):
        for job in list(self.queue):
            if not job.result.done():
                job.result.set_exception(ResourceClosed('Runtime no longer accepts work'))
            self._stop(job, 'quiescing')
        self.wake()

    async def aclose(self, deadline):
        self.closing = True
        self.quiesce()
        for job in list(self.jobs.values()):
            self._stop(job, 'closing')
        admissions = [job.admission for job in self.jobs.values() if job.admission is not None]
        if admissions:
            async with asyncio.timeout_at(deadline.when):
                await asyncio.gather(*admissions, return_exceptions=True)
        if self.reset_task and not self.reset_task.done():
            await asyncio.shield(self.reset_task)
        if self.backend == 'cpu' and self.pool:
            try:
                await self.pool.retire(deadline)
                for job in list(self.jobs.values()):
                    self._discard(job)
            except Exception:
                self.pool.emergency_kill()
                self.runtime.request_exit('cpu_pool_retirement_failed')
        elif self.backend == 'io':
            grace = min(deadline.when, self.runtime.loop.time() + self.runtime.config.cleanup_grace)
            while self._occupied() and self.runtime.loop.time() < grace:
                await asyncio.sleep(.01)
            if self._occupied():
                self.runtime.request_exit('io_cleanup_grace_expired')
            if self.pool:
                self.pool.shutdown(wait=False, cancel_futures=True)
                self.shutdown_called = True
        self.wake()

    @property
    def retired(self):
        if self.pool is None:
            return True
        if self.backend == 'cpu':
            return self.pool.retired
        return self.shutdown_called and not self._occupied()

    def snapshot(self):
        return {'state': 'closing' if self.closing else 'resetting' if self.frozen else 'running',
                'capacity': self.capacity, 'pending_capacity': self.pending,
                'generation': self.generation, 'resets': self.reset_count,
                **self.runtime.registry.snapshot()[self.backend]}
