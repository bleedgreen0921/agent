"""Attempt tool limits retain their leases while physical work is draining."""
import asyncio
from contextlib import asynccontextmanager
from execution.flow_config import tool_limits
from execution.infra.gate import CapacityGate
from execution.infra.types import CapacityTimeout, ProcessExitRequired
from execution.persistence import resources


class ToolCapacityExceeded(RuntimeError):
    pass


class ToolGate(CapacityGate):
    @asynccontextmanager
    async def borrow(self, deadline):
        if self.active >= self.capacity and len(self.queue) >= self.pending:
            raise ToolCapacityExceeded('Tool waiting capacity is full')
        async with super().borrow(deadline): yield


@asynccontextmanager
async def tool_slot(owner, deadline, *, limits=None, phase_id=None):
    runtime = resources()
    limits = limits or tool_limits()
    if not hasattr(runtime, 'tool_gate'):
        runtime.tool_gate = ToolGate(runtime, limits['worker'], limits['pending'])
        runtime.run_tool_gates = {}
    gate = runtime.run_tool_gates.setdefault(owner.attempt, ToolGate(runtime, limits['run'], limits['batch']))
    run_lease, worker_lease = gate.borrow(deadline), runtime.tool_gate.borrow(deadline)
    from execution.progress import ProgressStore
    from execution.scope import current_attempt
    if phase_id: await ProgressStore().waiting(current_attempt(), phase_id, 'tool_capacity')
    await run_lease.__aenter__()
    try:
        await worker_lease.__aenter__()
    except BaseException:
        await run_lease.__aexit__(None, None, None)
        raise
    try:
        if phase_id: await ProgressStore().waiting(current_attempt(), phase_id, None)
        yield
    finally:
        async def drain():
            until = runtime.loop.time() + runtime.config.cleanup_grace
            while runtime.registry.unfinished_owner(owner):
                if runtime.loop.time() >= until:
                    runtime.request_exit('tool_physical_cleanup_timeout')
                    raise ProcessExitRequired('Tool physical work did not stop')
                await asyncio.sleep(.01)
            await worker_lease.__aexit__(None, None, None)
            await run_lease.__aexit__(None, None, None)
            if not gate.active and not gate.queue:
                runtime.run_tool_gates.pop(owner.attempt, None)
        await runtime.cleanup(drain())
