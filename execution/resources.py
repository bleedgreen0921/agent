"""Thread-safe physical work registration, shared by sync and async runtimes."""
from threading import RLock
from uuid import uuid4
from execution.contracts import WorkHandle
from execution.infra.types import WorkRecord


class WorkRegistry:
    def __init__(self):
        self._work = {}
        self._lock = RLock()

    def register(self, handle: WorkHandle | WorkRecord):
        with self._lock:
            work_id = uuid4()
            if isinstance(handle, WorkRecord):
                handle.work_id = work_id
            self._work[work_id] = handle
            return work_id

    def release(self, work_id):
        with self._lock:
            handle = self._work[work_id]
            if handle.occupies_capacity:
                raise RuntimeError('Physical work still occupies Worker capacity')
            del self._work[work_id]

    def submit(self, work_id, generation=0):
        with self._lock:
            record = self._work[work_id]
            if record.state != 'queued' or record.stop_reason:
                return False
            record.state, record.generation = 'submitted', generation
            return True

    def stop(self, work_id, reason):
        with self._lock:
            record = self._work.get(work_id)
            if record is not None:
                record.stop_reason = record.stop_reason or reason
                return True
            return False

    def complete(self, work_id):
        """Only call from actual Future completion or confirmed process retirement."""
        with self._lock:
            record = self._work.get(work_id)
            if record is None or record.state == 'done':
                return False
            record.state = 'done'
            return True

    def stopped(self, work_id):
        with self._lock:
            return self._work[work_id].stop_reason is not None

    def snapshot(self):
        with self._lock:
            records = [r for r in self._work.values() if isinstance(r, WorkRecord)]
            return {backend: {'queued': sum(r.state == 'queued' for r in records if r.backend == backend),
                              'occupied': sum(r.occupies_capacity for r in records if r.backend == backend),
                              'stopped_unfinished': sum(r.stop_reason is not None and r.state != 'done' for r in records if r.backend == backend)}
                    for backend in ('io', 'cpu')}

    @property
    def occupied(self):
        with self._lock:
            return sum(handle.occupies_capacity for handle in self._work.values())

    def unfinished_attempt(self, owner):
        with self._lock:
            return sum(isinstance(r, WorkRecord) and r.owner is not None
                       and r.owner.attempt == owner.attempt and r.state != 'done'
                       for r in self._work.values())

    def unfinished_owner(self, owner):
        with self._lock:
            return sum(isinstance(r, WorkRecord) and r.owner == owner and r.state != 'done'
                       for r in self._work.values())
