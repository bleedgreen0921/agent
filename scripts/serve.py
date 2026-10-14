"""Supported personal API entry point: loopback, bounded process-level shutdown."""
import argparse
import asyncio
import importlib
import os
import threading
import uvicorn


class ExitWatchdog:
    """Independent of the event loop. This top-level module alone may force exit."""
    def __init__(self, server):
        self.server = server
        self.finished = threading.Event()
        self.thread = None
        self.resources = None

    def request(self, resources):
        resources.begin_shutdown()
        self.server.should_exit = True
        if self.thread is None:
            self.resources = resources
            self.thread = threading.Thread(target=self._watch, name='resource-exit-watchdog', daemon=True)
            self.thread.start()

    def _watch(self):
        if not self.finished.wait(self.resources.config.exit_grace):
            cpu = self.resources.cpu
            try:
                if cpu and cpu.pool:
                    cpu.pool.emergency_kill()
            finally:
                os._exit(70)

    def disarm_if_clean(self):
        if self.resources is None or self.resources.shutdown_complete:
            self.finished.set()
            return True
        return False


class ManagedServer(uvicorn.Server):
    async def shutdown(self, sockets=None):
        resources = getattr(self.config.app.state, 'resources', None)
        if resources is not None:
            resources.begin_shutdown()
            self.watchdog.request(resources)
        await super().shutdown(sockets=sockets)


async def serve(service, port):
    app = importlib.import_module(f'{service}_service.app').app
    server = ManagedServer(uvicorn.Config(app, host='127.0.0.1', port=port, proxy_headers=False))
    watchdog = ExitWatchdog(server)
    server.watchdog = watchdog
    app.state.resource_exit_handler = watchdog.request
    await server.serve()
    clean = watchdog.disarm_if_clean()
    if not clean:
        # Returning would enter ThreadPoolExecutor's unbounded atexit join. Keep
        # the loop alive until the process-level watchdog terminates this owner.
        while True:
            await asyncio.sleep(.1)
    return 1 if watchdog.resources and watchdog.resources.exit_reason else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=('agent', 'rag'))
    parser.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    port = args.port or (8002 if args.service == 'agent' else 8001)
    if not 1 <= port <= 65535:
        parser.error('--port must be in 1..65535')
    return asyncio.run(serve(args.service, port))


if __name__ == '__main__':
    raise SystemExit(main())
