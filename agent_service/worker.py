"""Parent Agent worker supervising one spawned process per Run."""

import logging
import multiprocessing as mp
import os
import time
from dataclasses import dataclass
from uuid import UUID

from agent_service.runtime import child_exited, claim_run, control_state, heartbeat, revoke_and_finish


log = logging.getLogger(__name__)


@dataclass
class Child:
    process: mp.Process
    token: UUID
    last_heartbeat: float


def child_main(run_id: UUID, token: UUID) -> None:
    from agent_service.execution import execute_run

    execute_run(run_id, token)


def terminate(child: Child) -> None:
    if child.process.is_alive():
        child.process.terminate()
        child.process.join(timeout=float(os.environ.get("AGENT_TERMINATE_GRACE_SECONDS", "3")))
    if child.process.is_alive():
        child.process.kill()
        child.process.join(timeout=2)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    context = mp.get_context("spawn")
    children: dict[UUID, Child] = {}
    slots = int(os.environ.get("AGENT_MAX_CONCURRENT_RUNS", "2"))
    heartbeat_interval = float(os.environ.get("AGENT_HEARTBEAT_SECONDS", "30"))
    poll = float(os.environ.get("AGENT_CONTROL_POLL_SECONDS", "0.5"))
    try:
        while True:
            now = time.monotonic()
            for run_id, child in list(children.items()):
                state = control_state(run_id, child.token)
                if state in {"cancel", "timeout", "lost"}:
                    if state != "lost":
                        revoke_and_finish(run_id, child.token, state)
                    terminate(child)
                    children.pop(run_id, None)
                    continue
                if not child.process.is_alive():
                    child.process.join(timeout=1)
                    child_exited(run_id, child.token)
                    children.pop(run_id, None)
                    continue
                if now - child.last_heartbeat >= heartbeat_interval:
                    if not heartbeat(run_id, child.token):
                        terminate(child)
                        children.pop(run_id, None)
                        continue
                    child.last_heartbeat = now
            while len(children) < slots:
                claimed = claim_run()
                if not claimed:
                    break
                process = context.Process(target=child_main, args=(claimed["run_id"], claimed["lease_token"]), name=f"agent-run-{claimed['run_id']}")
                process.start()
                children[claimed["run_id"]] = Child(process, claimed["lease_token"], time.monotonic())
            time.sleep(poll)
    finally:
        for child in children.values():
            terminate(child)


if __name__ == "__main__":
    main()
