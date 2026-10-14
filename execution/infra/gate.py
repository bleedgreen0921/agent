"""Coroutine-local bounded FIFO admission; no background admission Tasks."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from execution.infra.types import CapacityTimeout


class CapacityGate:
    def __init__(self, runtime, capacity, pending):
        self.runtime, self.capacity, self.pending = runtime, capacity, pending
        self.active = 0
        self.queue = deque()
        self.changed = asyncio.Event()

    def wake(self):
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    @asynccontextmanager
    async def borrow(self, deadline):
        ticket = None
        acquired = False
        try:
            async with asyncio.timeout_at(deadline.when):
                while True:
                    self.runtime.check()
                    if deadline.remaining() <= 0:
                        raise CapacityTimeout('Capacity admission deadline expired')
                    at_front = ticket is not None and self.queue[0] is ticket
                    if self.active < self.capacity and (at_front or not self.queue):
                        if ticket is not None:
                            self.queue.popleft()
                            ticket = None
                        self.active += 1
                        acquired = True
                        self.wake()
                        break
                    if ticket is None and len(self.queue) < self.pending:
                        ticket = object()
                        self.queue.append(ticket)
                    await self.changed.wait()
            yield
        except TimeoutError as exc:
            if not acquired:
                raise CapacityTimeout('Capacity admission deadline expired') from exc
            raise
        finally:
            if ticket is not None:
                self.queue.remove(ticket)
            if acquired:
                self.active -= 1
            self.wake()

    def snapshot(self):
        return {'capacity': self.capacity, 'pending_capacity': self.pending,
                'occupied': self.active, 'queued': len(self.queue)}
