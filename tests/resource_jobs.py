"""Importable spawn inputs and isolated process probes; no business state."""
import os
import time


def identity(value):
    return os.getpid(), value


def wait_file(path):
    with open(path + '.started', 'w') as f:
        f.write(str(os.getpid()))
    while not os.path.exists(path):
        time.sleep(.005)
    return os.getpid()


def crash():
    os._exit(31)


def stuck():
    while True:
        time.sleep(1)


def child_main(scenario):
    import asyncio
    from dataclasses import replace
    import threading
    from execution.infra.runtime import RuntimeResources
    from execution.infra.types import ResourceConfig, OperationDeadline
    from scripts.serve import ExitWatchdog
    os.environ.setdefault('AGENT_DATABASE_URL', 'postgresql://127.0.0.1:9/unavailable')
    async def run():
        if scenario in ('watchdog', 'watchdog-blocked'):
            class Server:
                should_exit = False
            watchdog = ExitWatchdog(Server())
            config = replace(ResourceConfig(), cleanup_grace=.1, exit_grace=.5)
            async with RuntimeResources('agent-api', config=config, on_exit=watchdog.request) as resources:
                started = threading.Event()
                def forever():
                    started.set()
                    threading.Event().wait()
                task = asyncio.create_task(resources.io.run(forever, deadline=OperationDeadline.after(3)))
                while not started.is_set():
                    await asyncio.sleep(.005)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await resources.exit_requested.wait()
                if scenario == 'watchdog-blocked':
                    time.sleep(60)
            while True:
                await asyncio.sleep(.05)
    asyncio.run(run())




def stubborn_descendant(path):
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, '-c',
        'import os,signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        'open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)', path + '.child'])
    while not os.path.exists(path + '.child'):
        time.sleep(.005)
    with open(path + '.started', 'w') as f:
        f.write(str(os.getpid()))
    while True:
        time.sleep(1)


def group_probe(path):
    """Subreaper is confined to this test subprocess so descendants are reaped."""
    import asyncio
    import ctypes
    import signal
    from dataclasses import replace
    from execution.infra.runtime import RuntimeResources
    from execution.infra.types import ResourceConfig, OperationDeadline
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    os.environ.setdefault('AGENT_DATABASE_URL', 'postgresql://127.0.0.1:9/unavailable')
    from tests.resource_jobs import stubborn_descendant as cpu_function
    async def run():
        async with RuntimeResources('agent-worker', config=replace(ResourceConfig(), cleanup_grace=.03)) as r:
            task = asyncio.create_task(r.cpu.run(cpu_function, path, deadline=OperationDeadline.after(5)))
            async with asyncio.timeout(3):
                while not os.path.exists(path + '.started'):
                    await asyncio.sleep(.005)
            parent = int(open(path + '.started').read())
            child = int(open(path + '.child').read())
            try:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                async with asyncio.timeout(3):
                    while r.cpu.generation == 0:
                        await asyncio.sleep(.005)
                async with asyncio.timeout(2):
                    while True:
                        pid, status = os.waitpid(child, os.WNOHANG)
                        if pid:
                            assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
                            break
                        await asyncio.sleep(.005)
                try:
                    os.killpg(parent, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError('old CPU process group survived')
            finally:
                try:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
    asyncio.run(run())


if __name__ == '__main__':
    import sys
    if sys.argv[1] == 'cpu-group':
        group_probe(sys.argv[2])
    else:
        child_main(sys.argv[1])


def simple_document(path,media_type,title,max_chunks):
    from pathlib import Path
    from rag_service.parse import ParseFailure
    content=Path(path).read_text().strip()
    if not content:raise ParseFailure('EMPTY_CONTENT')
    return [{'content':content,'heading_path':[],'locator':{'kind':'txt','line_start':1,'line_end':1},'table_html':None,'search_text':content,'embedding_input':content}],[]
