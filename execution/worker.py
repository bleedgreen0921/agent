"""Async attempt supervision. Physical work, leases, and logical Tasks are distinct."""
import asyncio
import logging
import signal
from dataclasses import dataclass,field
from uuid import uuid4
import psycopg

from execution.stores import TaskStore
from execution.contracts import TaskKind,TaskRef
from execution.store import control_refusal
from execution.infra.runtime import RuntimeResources
from execution.infra.types import WorkOwner,OperationDeadline,ResourceError
from execution.persistence import bind,PersistenceOutcomeUnknown
from execution.scope import AttemptContext
from execution.outcomes import OutcomeKind
from execution import pipelines
from db.worker_status import beat,remove

log=logging.getLogger(__name__)


@dataclass
class AttemptHandle:
    owner: WorkOwner
    job: dict
    deadline: OperationDeadline
    stopped: asyncio.Event=field(default_factory=asyncio.Event)
    task: asyncio.Task | None=None
    last_renew: float=0
    last_progress_check: float=0
    settled: bool=False
    outcome: object=None

    @property
    def context(self):
        return AttemptContext(self.owner.task,self.owner.lease_token,self.stopped,self.deadline)


async def settle(handle, *, failure=None, retryable=False):
    return await pipelines.finish(handle.context, handle.job, failure=failure, retryable=retryable)


async def execute(handle, runtime):
    try:
        handle.outcome = await pipelines.execute(handle.context, handle.job, runtime)
        handle.settled = handle.outcome.kind == OutcomeKind.FINISHED
    finally:
        runtime.cancel_attempt(handle.owner)


async def stop(handle,runtime):
    handle.stopped.set()
    runtime.cancel_attempt(handle.owner)
    if not handle.task.done():handle.task.cancel()


async def control(handle,runtime,now):
    row = await TaskStore(runtime).observe_control(handle.owner.task)
    refusal=control_refusal(handle.owner.task,handle.owner.lease_token,row)
    if refusal is not None and refusal not in {'user_cancel','deadline','lease_expired'}:
        await stop(handle,runtime);return
    if refusal is not None:
        await stop(handle,runtime)
        await settle(handle)
        return
    from execution.progress import ProgressStore
    from execution.flow_config import number
    if now - handle.last_progress_check >= number('PROGRESS_POLL_SECONDS', 5):
        handle.last_progress_check = now
        if await ProgressStore().inspect(handle.owner.task, handle.owner.lease_token):
            handle.settled = True
            await stop(handle, runtime)
            return
    interval=10 if handle.owner.task.kind==TaskKind.DOCUMENT else float(__import__('os').environ.get('AGENT_HEARTBEAT_SECONDS','30')) if handle.owner.task.kind==TaskKind.AGENT else 30
    lease=60 if handle.owner.task.kind==TaskKind.DOCUMENT else 300 if handle.owner.task.kind==TaskKind.MEMORY else int(__import__('os').environ.get('AGENT_LEASE_SECONDS','120'))
    if now-handle.last_renew>=interval:
        if not await TaskStore(runtime).renew(handle.owner.task,handle.owner.lease_token,lease):await stop(handle,runtime)
        handle.last_renew=now


async def run(service):
    import os
    from db.config_check import validate_role
    from scripts.serve import ExitWatchdog
    validate_role(service+'-worker')
    stopping=asyncio.Event()
    loop=asyncio.get_running_loop()
    class Host:
        @property
        def should_exit(self):return stopping.is_set()
        @should_exit.setter
        def should_exit(self,value):
            if value:stopping.set()
    watchdog=ExitWatchdog(Host())
    runtime=RuntimeResources(service+'-worker',on_exit=watchdog.request)
    for sig in (signal.SIGTERM,signal.SIGINT):
        def request():
            runtime.begin_shutdown();watchdog.request(runtime);stopping.set()
        loop.add_signal_handler(sig,request)
    handles={};instance=uuid4()
    specs=[(TaskKind.AGENT,int(os.environ.get('AGENT_MAX_CONCURRENT_RUNS','2'))),(TaskKind.MEMORY,1)] if service=='agent' else [(TaskKind.DOCUMENT,int(os.environ.get('RAG_MAX_CONCURRENT_DOCUMENTS','1')))]
    if any(capacity<=0 for _,capacity in specs):raise ValueError('Worker concurrency must be positive')
    last_beat=-float('inf');backoff=.5
    try:
        async with runtime:
            with bind(runtime):
                if service == 'rag': await runtime.http.register_available()
                if service == 'agent':
                    from agent_service.startup import initialize
                    await initialize(runtime)
                try:
                    while not stopping.is_set():
                        try:
                            now=loop.time()
                            for key,handle in list(handles.items()):
                                if not handle.task.done():await control(handle,runtime,now)
                                if handle.task.done():
                                    if not handle.task.cancelled():handle.task.exception()
                                    if not handle.settled:
                                        await settle(handle);handle.settled=True
                                    if not runtime.registry.unfinished_attempt(handle.owner):
                                        runtime.stopped_attempts.discard(handle.owner.attempt)
                                        if hasattr(runtime, 'run_tool_gates'): runtime.run_tool_gates.pop(handle.owner.attempt, None)
                                        del handles[key]
                            if now-last_beat>=10:
                                await beat(service,instance,memory_consumer_alive=not stopping.is_set(), resource_state={**runtime.snapshot(),
                                    'draining_tasks': [str(h.owner.task.id) for h in handles.values() if h.task.done() and runtime.registry.unfinished_attempt(h.owner)],
                                    'draining_attempts': sum(h.task.done() and bool(runtime.registry.unfinished_attempt(h.owner)) for h in handles.values())})
                                from execution.admission import AdmissionStore
                                await AdmissionStore().status('agent_run' if service=='agent' else 'rag_document')
                                last_beat=now
                            for kind,slots in specs:
                                while sum(h.owner.task.kind==kind for h in handles.values())<slots:
                                    start=loop.time()
                                    job=await TaskStore(runtime).claim(kind,instance)
                                    if not job:break
                                    task_id=job['run_id'] if kind==TaskKind.AGENT else job['id']
                                    token=job.get('lease_token',job.get('token'))
                                    limit=OperationDeadline(start+float(job['remaining_seconds']))
                                    owner=WorkOwner(TaskRef(task_id,kind),token)
                                    handle=AttemptHandle(owner,job,limit,last_renew=loop.time())
                                    handles[owner.attempt]=handle
                                    handle.task=loop.create_task(execute(handle,runtime),name=f'{kind}:{task_id}')
                            backoff=.5
                            try:await asyncio.wait_for(stopping.wait(),timeout=float(os.environ.get('AGENT_CONTROL_POLL_SECONDS','.5')) if service=='agent' else 1)
                            except TimeoutError:pass
                        except (psycopg.Error,PersistenceOutcomeUnknown,ResourceError):
                            log.warning('Worker control unavailable; stop local attempts')
                            for handle in handles.values():await stop(handle,runtime)
                            try:await asyncio.wait_for(stopping.wait(),timeout=backoff)
                            except TimeoutError:pass
                            backoff=min(backoff*2,30)
                except asyncio.CancelledError:
                    if not stopping.is_set() and not runtime.exit_reason:raise
                finally:
                    shutdown=runtime.begin_shutdown();watchdog.request(runtime)
                    for handle in handles.values():await stop(handle,runtime)
                    if handles:
                        tasks=[h.task for h in handles.values()]
                        _,pending=await asyncio.wait(tasks,timeout=shutdown.remaining())
                        if pending:runtime.request_exit('attempt_cleanup_timeout')
                    with bind(runtime,deadline=shutdown,cleanup=True):
                        for handle in handles.values():
                            try:await settle(handle)
                            except (psycopg.Error,ResourceError,PersistenceOutcomeUnknown,TimeoutError):pass
                        try:await remove(service,instance)
                        except (psycopg.Error,ResourceError,PersistenceOutcomeUnknown,TimeoutError):pass
    finally:
        for sig in (signal.SIGTERM,signal.SIGINT):loop.remove_signal_handler(sig)
        if not watchdog.disarm_if_clean():
            while True:await asyncio.sleep(.1)
    return 1 if runtime.exit_reason else 0
