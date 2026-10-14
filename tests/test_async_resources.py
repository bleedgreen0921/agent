"""Deterministic local infrastructure tests; asyncio.run owns each test loop."""
import asyncio
import contextvars
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

import httpx
import pytest
from execution.contracts import TaskKind, TaskRef
from execution.infra.runtime import RuntimeResources
from execution.infra.types import (OperationDeadline as Deadline, ResourceConfig, ResourceClosed,
                                  CapacityTimeout, CpuPoolReset, WorkOwner, WorkStopped)
from tests.resource_jobs import identity, wait_file, crash
from scripts.integration_pytest import allow_test_endpoint, no_real_http


@pytest.fixture(autouse=True)
def resource_env(monkeypatch):
    if not os.environ.get('AGENT_DATABASE_URL'):
        monkeypatch.setenv('AGENT_DATABASE_URL', 'postgresql://127.0.0.1:9/unavailable')


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


def config(**kwargs):
    return replace(ResourceConfig(), **kwargs)


def test_running_thread_cancellation_keeps_capacity_queue_cancel_never_executes():
    async def check():
        started, finish, executed = threading.Event(), threading.Event(), []
        def blocking():
            started.set()
            finish.wait(3)
            return 10
        async with RuntimeResources('agent-worker', config=config(io_workers=1, io_pending=1)) as r:
            first = asyncio.create_task(r.io.run(blocking, deadline=Deadline.after(4)))
            await eventually(started.is_set)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            assert r.snapshot()['io']['occupied'] == 1
            assert r.snapshot()['io']['stopped_unfinished'] == 1
            queued = asyncio.create_task(r.io.run(lambda: executed.append(1), deadline=Deadline.after(4)))
            await eventually(lambda: r.snapshot()['io']['queued'] == 1)
            with pytest.raises(CapacityTimeout):
                await r.io.run(lambda: executed.append(2), deadline=Deadline.after(.03))
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
            assert executed == [] and r.registry.occupied == 1
            finish.set()
            await eventually(lambda: r.registry.occupied == 0)
            assert await r.io.run(lambda: 42, deadline=Deadline.after(1)) == 42
            await eventually(lambda: not r.io.jobs)
    asyncio.run(check())


def test_attempt_tokens_context_copy_and_quiesce_wakes_admission():
    value = contextvars.ContextVar('resource-test')
    async def check():
        task = TaskRef(uuid4(), TaskKind.AGENT)
        old, new = WorkOwner(task, uuid4()), WorkOwner(task, uuid4())
        started, finish = threading.Event(), threading.Event()
        async with RuntimeResources('agent-worker', config=config(io_workers=1, io_pending=1)) as r:
            value.set('captured')
            assert await r.io.run(value.get, owner=new, deadline=Deadline.after(1)) == 'captured'
            r.cancel_attempt(old)
            with pytest.raises(WorkStopped):
                await r.io.run(lambda: 1, owner=old, deadline=Deadline.after(1))
            def blocking():
                started.set()
                finish.wait(3)
            running = asyncio.create_task(r.io.run(blocking, owner=new, deadline=Deadline.after(4)))
            await eventually(started.is_set)
            queued = asyncio.create_task(r.io.run(lambda: 2, owner=new, deadline=Deadline.after(4)))
            await eventually(lambda: len(r.io.queue) == 1)
            admission = asyncio.create_task(r.io.run(lambda: 3, deadline=Deadline.after(4)))
            await asyncio.sleep(.01)
            r.quiesce()
            outcomes = await asyncio.gather(queued, admission, return_exceptions=True)
            assert all(isinstance(v, (ResourceClosed, RuntimeError)) for v in outcomes)
            finish.set()
            await running
        assert r.registry.occupied == 0
    asyncio.run(check())


def test_completion_cancellation_races_release_once():
    async def check():
        async with RuntimeResources('agent-api', config=config(io_workers=1)) as r:
            for _ in range(50):
                task = asyncio.create_task(r.io.run(lambda: 1, deadline=Deadline.after(1)))
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await eventually(lambda: not r.io.jobs)
            assert r.registry.occupied == 0
    asyncio.run(check())


def test_unfinished_thread_requests_exit_without_replacement():
    async def check():
        started, finish = threading.Event(), threading.Event()
        r = RuntimeResources('agent-api', config=config(io_workers=1, cleanup_grace=.03))
        async with r:
            def blocking():
                started.set()
                finish.wait(3)
            pending = asyncio.create_task(r.io.run(blocking, deadline=Deadline.after(2)))
            await eventually(started.is_set)
            pool = r.io.pool
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await asyncio.wait_for(r.exit_requested.wait(), 1)
            assert r.exit_reason == 'io_cleanup_grace_expired'
            assert r.io.pool is pool and r.registry.occupied == 1
            with pytest.raises(ResourceClosed):
                await r.io.run(lambda: 1, deadline=Deadline.after(1))
            finish.set()
        assert (await r.aclose())['unfinished'] == 0
    asyncio.run(check())


def test_cpu_pool_reuse_cancel_reset_preserves_unsubmitted_work(tmp_path):
    async def check():
        async with RuntimeResources('agent-worker', config=config(cpu_workers=2, cleanup_grace=.05)) as r:
            pid, _ = await r.cpu.run(identity, 'first', deadline=Deadline.after(5))
            pids = {p.pid for p in r.cpu.pool.processes}
            pid2, _ = await r.cpu.run(identity, 'second', deadline=Deadline.after(5))
            assert pid in pids and pid2 in pids and r.cpu.generation == 0
            paths = [str(tmp_path / name) for name in ('a', 'b')]
            first = asyncio.create_task(r.cpu.run(wait_file, paths[0], deadline=Deadline.after(5)))
            other = asyncio.create_task(r.cpu.run(wait_file, paths[1], deadline=Deadline.after(5)))
            await eventually(lambda: all(Path(p + '.started').exists() for p in paths))
            queued = asyncio.create_task(r.cpu.run(identity, 'after', deadline=Deadline.after(5)))
            await eventually(lambda: len(r.cpu.queue) == 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            with pytest.raises(CpuPoolReset):
                await other
            new_pid, result = await queued
            assert result == 'after' and new_pid not in pids and r.cpu.generation == 1
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            assert r.cpu.reset_count == 1
        assert r.registry.occupied == 0
    asyncio.run(check())


def test_cpu_crash_reset_and_loop_progress():
    async def check():
        async with RuntimeResources('agent-worker', config=config(cleanup_grace=.03)) as r:
            with pytest.raises(CpuPoolReset):
                await r.cpu.run(crash, deadline=Deadline.after(5))
            await eventually(lambda: not r.cpu.frozen)
            assert (await r.cpu.run(identity, 42, deadline=Deadline.after(5)))[1] == 42
            with pytest.raises(ValueError):
                await r.cpu.run(lambda: 1, deadline=Deadline.after(1))
    asyncio.run(check())


def test_close_idempotent_caller_cancellation_and_affinity():
    resources = []
    async def check():
        r = RuntimeResources('agent-api')
        await r.__aenter__()
        resources.append(r)
        closing = asyncio.create_task(r.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        a, b = await asyncio.gather(r.aclose(), r.aclose())
        assert a is b and a['state'] == 'CLOSED'
    asyncio.run(check())
    async def wrong_loop():
        with pytest.raises(ResourceClosed, match='event loop'):
            resources[0].snapshot()
    asyncio.run(wrong_loop())
    r = resources[0]
    r.pid -= 1
    async def wrong_pid():
        with pytest.raises(ResourceClosed, match='process'):
            r.snapshot()
    asyncio.run(wrong_pid())


@pytest.mark.parametrize('scenario', ['watchdog', 'watchdog-blocked'])
def test_watchdog_forces_nonzero_exit_with_permanently_stuck_thread(scenario):
    result = subprocess.run([sys.executable, '-m', 'tests.resource_jobs', scenario],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 70, result.stderr.decode()


def test_mock_http_request_headers_clients_and_stream_cleanup():
    async def check():
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, text='hello')
        async with RuntimeResources('agent-api', http_transports={'agent-model': httpx.MockTransport(handler)}) as r:
            for token in ('secret-first-token', 'secret-second-token'):
                response = await r.http.request('agent-model', 'GET', 'https://model.invalid/',
                                               deadline=Deadline.after(1), headers={'Authorization': token})
                assert response.content == b'hello' and response.is_closed
            assert [v.headers['Authorization'] for v in requests] == ['secret-first-token', 'secret-second-token']
            assert len(r.http.clients) == 1 and not r.http.clients['agent-model'].trust_env
            async with r.http.stream('agent-model', 'GET', 'https://model.invalid/', deadline=Deadline.after(1)) as response:
                assert r.snapshot()['http']['agent-model']['occupied'] == 1
            assert response.is_closed and r.snapshot()['http']['agent-model']['occupied'] == 0
            assert 'secret-first-token' not in str(r.snapshot())
    asyncio.run(check())


def test_loopback_http_reuse_concurrency_deadline_stream_cancel():
    async def check():
        connections, writers, started = [], set(), asyncio.Event()
        async def handler(reader, writer):
            connections.append(writer.get_extra_info('peername'))
            writers.add(writer)
            try:
                while True:
                    header = await reader.readuntil(b'\r\n\r\n')
                    path = header.split(b' ')[1]
                    if path == b'/slow':
                        started.set()
                        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx')
                        await writer.drain()
                        await reader.read()  # cancellation must close this connection
                        return
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                writers.discard(writer)
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with allow_test_endpoint('127.0.0.1', port):
                async with RuntimeResources('agent-api', config=config(http_concurrency=1, http_pending=1)) as r:
                    base = f'http://127.0.0.1:{port}'
                    client = None
                    for _ in range(2):
                        assert (await r.http.request('rag', 'GET', base, deadline=Deadline.after(1))).text == 'ok'
                        client = client or r.http.clients['rag']
                        assert client is r.http.clients['rag']
                    assert len(connections) == 1
                    async def stream():
                        async with r.http.stream('rag', 'GET', base + '/slow', deadline=Deadline.after(2)) as resp:
                            await resp.aread()
                    stream_task = asyncio.create_task(stream())
                    await started.wait()
                    with pytest.raises(CapacityTimeout):
                        await r.http.request('rag', 'GET', base, deadline=Deadline.after(.03))
                    assert r.snapshot()['http']['rag']['occupied'] == 1
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                    assert r.snapshot()['http']['rag']['occupied'] == 0
                    assert (await r.http.request('rag', 'GET', base, deadline=Deadline.after(1))).text == 'ok'
                    with pytest.raises(TimeoutError):
                        await r.http.request('rag', 'GET', base + '/slow', deadline=Deadline.after(.03))
        finally:
            server.close()
            await server.wait_closed()
            for writer in tuple(writers):
                writer.close()
            await eventually(lambda: not writers)
    asyncio.run(check())


def test_exact_test_http_exception_does_not_allow_other_ports():
    with no_real_http() as attempts:
        with allow_test_endpoint('127.0.0.1', 32123):
            with httpx.Client() as client, pytest.raises(AssertionError):
                client.get('http://127.0.0.1:32124')
    assert attempts == ['127.0.0.1']


@pytest.mark.parametrize('name,value', [('ASYNC_IO_WORKERS','0'), ('ASYNC_DB_ACQUIRE_TIMEOUT_SECONDS','nan'),
                                      ('ASYNC_CPU_PENDING','-1'), ('ASYNC_HTTP_KEEPALIVE','17')])
def test_configuration_rejects_invalid_capacity(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        ResourceConfig.from_env()


def test_cpu_loop_progress_and_reset_failure_blocks_submission(tmp_path, monkeypatch):
    async def check():
        async with RuntimeResources('agent-worker', config=config(cleanup_grace=.03, cpu_reset_timeout=.1)) as r:
            path = str(tmp_path / 'compute')
            work = asyncio.create_task(r.cpu.run(wait_file, path, deadline=Deadline.after(5)))
            await eventually(lambda: Path(path + '.started').exists())
            # This loop progresses while CPU work occupies its only slot.
            ticks = 0
            for _ in range(5):
                await asyncio.sleep(.005)
                ticks += 1
            assert ticks == 5 and r.registry.occupied == 1
            old = r.cpu.pool
            real_retire = old.retire
            async def fail(deadline, force=True):
                raise TimeoutError('injected retirement failure')
            monkeypatch.setattr(old, 'retire', fail)
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await asyncio.wait_for(r.exit_requested.wait(), 1)
            assert r.cpu.pool is old and r.cpu.generation == 0 and r.cpu.frozen
            with pytest.raises(ResourceClosed):
                await r.cpu.run(identity, 1, deadline=Deadline.after(1))
            monkeypatch.setattr(old, 'retire', real_retire)
        assert old.retired and r.registry.occupied == 0
    asyncio.run(check())


def test_partial_startup_failure_rolls_back_and_close_has_one_deadline(monkeypatch):
    from execution.infra.database import AsyncDatabase
    original = AsyncDatabase.open
    async def failing_open(self):
        await original(self)
        raise RuntimeError('injected startup failure')
    monkeypatch.setattr(AsyncDatabase, 'open', failing_open)
    async def check():
        r = RuntimeResources('agent-worker')
        with pytest.raises(RuntimeError, match='injected'):
            await r.__aenter__()
        assert r.state == 'CLOSED' and all(p.closed for p in r.db.pools.values())
        monkeypatch.setattr(AsyncDatabase, 'open', original)
        r = RuntimeResources('agent-worker', config=config(exit_grace=.08))
        await r.__aenter__()
        limits = []
        async def slow(deadline):
            limits.append(deadline.when)
            await asyncio.sleep(.05)
        monkeypatch.setattr(r.cpu, 'aclose', slow)
        monkeypatch.setattr(r.io, 'aclose', slow)
        started = asyncio.get_running_loop().time()
        report = await r.aclose()
        elapsed = asyncio.get_running_loop().time() - started
        assert .07 <= elapsed < .3 and limits[0] == limits[1]
        assert report['exit_reason'] and report['failures'] == ['TimeoutError']
        from scripts.serve import ExitWatchdog
        watchdog = ExitWatchdog(None)
        watchdog.resources = r
        assert not report['shutdown_complete'] and not watchdog.disarm_if_clean()
        # The deadline expired before dependent pools could be destroyed. Finish
        # test-owned housekeeping explicitly; the production owner exits here.
        await r.db.aclose(Deadline.after(1))
    asyncio.run(check())


def test_repeated_stream_cancellation_keeps_capacity_until_response_closed():
    async def check():
        closing, finish = asyncio.Event(), asyncio.Event()
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'first'
                await asyncio.Event().wait()
            async def aclose(self):
                closing.set()
                await finish.wait()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
        async with RuntimeResources('agent-api', http_transports={'rag': transport}, config=config(http_concurrency=1)) as r:
            async def read():
                async with r.http.stream('rag', 'GET', 'https://mock.invalid', deadline=Deadline.after(2)) as resp:
                    await resp.aread()
            task = asyncio.create_task(read())
            await eventually(lambda: r.snapshot()['http'].get('rag', {}).get('occupied') == 1)
            await asyncio.sleep(.01)
            task.cancel()
            await closing.wait()
            task.cancel()
            await asyncio.sleep(.01)
            assert r.snapshot()['http']['rag']['occupied'] == 1 and not task.done()
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
            assert r.snapshot()['http']['rag']['occupied'] == 0
    asyncio.run(check())


def test_quiesce_http_pending_rejects_and_close_cancels_stream():
    async def check():
        started, closed = asyncio.Event(), asyncio.Event()
        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b''
            async def aclose(self):
                closed.set()
        async with RuntimeResources('agent-api', config=config(http_concurrency=1, http_pending=1),
                                    http_transports={'rag': httpx.MockTransport(lambda req: httpx.Response(200, stream=WaitingStream()))}) as r:
            running = asyncio.create_task(r.http.request('rag', 'GET', 'https://mock.invalid', deadline=Deadline.after(4)))
            await started.wait()
            waiting = asyncio.create_task(r.http.request('rag', 'GET', 'https://mock.invalid', deadline=Deadline.after(4)))
            await eventually(lambda: r.snapshot()['http']['rag']['queued'] == 1)
            r.quiesce()
            assert isinstance((await asyncio.gather(waiting, return_exceptions=True))[0], ResourceClosed)
            report = await r.aclose()
            assert closed.is_set() and report['unfinished'] == 0
            assert isinstance((await asyncio.gather(running, return_exceptions=True))[0], asyncio.CancelledError)
            assert r.http.clients['rag'].is_closed and all(p.closed for p in r.db.pools.values())
    asyncio.run(check())


def test_unsupported_cpu_platform_and_unserializable_inputs_are_rejected(monkeypatch):
    from types import SimpleNamespace
    from execution.infra import cpu_backend
    from execution.infra.types import ResourceUnavailable
    monkeypatch.setattr(cpu_backend, 'sys', SimpleNamespace(implementation=SimpleNamespace(name='pypy'),
                                                          version_info=(3,12), platform='linux'))
    async def check():
        async with RuntimeResources('agent-worker') as r:
            with pytest.raises(ResourceUnavailable, match='CPython'):
                await r.cpu.run(identity, 1, deadline=Deadline.after(1))
            with pytest.raises((TypeError, ValueError)):
                await r.cpu.run(identity, threading.Lock(), deadline=Deadline.after(1))
            assert r.registry.occupied == 0 and r.cpu.pool is None
    asyncio.run(check())


def test_callback_after_event_loop_closed_only_releases_actual_completion():
    started, finish = threading.Event(), threading.Event()
    async def setup():
        r = RuntimeResources('agent-worker', config=config(cleanup_grace=.01))
        await r.__aenter__()
        def blocking():
            started.set()
            finish.wait(3)
        task = asyncio.create_task(r.io.run(blocking, deadline=Deadline.after(2)))
        await eventually(started.is_set)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        report = await r.aclose()
        assert report['unfinished'] == 1
        return r
    loop = asyncio.new_event_loop()
    r = loop.run_until_complete(setup())
    loop.close()
    assert r.registry.occupied == 1
    job = next(iter(r.io.jobs.values()))
    finish.set()
    job.physical.result(timeout=1)
    import time
    limit = time.monotonic() + 1
    while r.registry.occupied and time.monotonic() < limit:
        threading.Event().wait(.001)
    assert r.registry.occupied == 0


@pytest.mark.parametrize('service', ['agent', 'rag'])
def test_supported_api_entrypoint_health_and_graceful_shutdown_without_database(service):
    import socket
    import time
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env['AGENT_DATABASE_URL'] = env['RAG_DATABASE_URL'] = 'postgresql://127.0.0.1:9/unavailable'
    env['RAG_SERVICE_TOKEN'] = 'synthetic-entrypoint-token-' + 's' * 43
    process = subprocess.Popen([sys.executable, '-m', 'scripts.serve', '--service', service, '--port', str(port)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = b''
    try:
        deadline = time.monotonic() + 10
        with allow_test_endpoint('127.0.0.1', port), httpx.Client(trust_env=False, timeout=.2) as client:
            while True:
                try:
                    response = client.get(f'http://127.0.0.1:{port}/health')
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline or process.poll() is not None:
                    pytest.fail('supported API entrypoint failed to start')
                time.sleep(.03)
            assert response.json() == {'status': 'ok', 'service': service}
        process.terminate()
        output, _ = process.communicate(timeout=5)
        assert process.returncode in (0, -15), output.decode()
        assert b'Application shutdown complete' in output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2)


def test_io_rejects_async_clients_and_async_callables():
    async def check():
        async with RuntimeResources('agent-worker') as r, httpx.AsyncClient() as client:
            startup_pool = r.io.pool
            with pytest.raises(ValueError, match='connections and clients'):
                await r.io.run(lambda x: x, {'nested': [client]}, deadline=Deadline.after(1))
            with pytest.raises(ValueError, match='synchronous'):
                await r.io.run(client.aclose, deadline=Deadline.after(1))
            assert r.io.pool is startup_pool and r.registry.occupied == 0
    asyncio.run(check())


def test_expired_queued_work_is_rejected_before_timeout_coroutine_resumes():
    import time
    async def check():
        started, finish, executed = threading.Event(), threading.Event(), []
        async with RuntimeResources('agent-worker', config=config(io_workers=1)) as r:
            def blocking():
                started.set()
                finish.wait(2)
            running = asyncio.create_task(r.io.run(blocking, deadline=Deadline.after(2)))
            await eventually(started.is_set)
            queued = asyncio.create_task(r.io.run(lambda: executed.append(1), deadline=Deadline.after(.03)))
            await eventually(lambda: len(r.io.queue) == 1)
            finish.set()
            # Give the physical completion callback a place in the ready queue
            # before the expired caller's timeout callback gets to run.
            time.sleep(.06)
            await running
            with pytest.raises(CapacityTimeout):
                await queued
            assert executed == []
            await eventually(lambda: not r.io.jobs)
    asyncio.run(check())


def test_already_expired_deadline_never_dispatches_work():
    async def check():
        executed = []
        def handler(request):
            executed.append('http')
            return httpx.Response(200)
        async with RuntimeResources('agent-worker', http_transports={'rag': httpx.MockTransport(handler)}) as r:
            expired = Deadline(asyncio.get_running_loop().time() - 1)
            startup_pool = r.io.pool
            with pytest.raises(CapacityTimeout):
                await r.io.run(lambda: executed.append('io'), deadline=expired)
            with pytest.raises(CapacityTimeout):
                await r.http.request('rag', 'GET', 'https://mock.invalid', deadline=expired)
            with pytest.raises(CapacityTimeout):
                async with r.db.transaction('normal', expired):
                    pytest.fail('expired transaction admitted')
            assert executed == [] and r.io.pool is startup_pool and r.registry.occupied == 0
    asyncio.run(check())


def test_cpu_retirement_kills_descendant_after_group_leader_exits(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'tests.resource_jobs', 'cpu-group', str(tmp_path / 'group')],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
    assert result.returncode == 0, result.stderr.decode()
