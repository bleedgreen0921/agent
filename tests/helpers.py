from datetime import datetime, timedelta, timezone
from uuid import uuid4
import hashlib
from db.connection import connect
from execution import store
from execution.contracts import TaskRef, TaskKind
from agent_service.versions import GRAPH_VERSION


def insert_run(*, status='queued', created_delta=timedelta(), queue_delta=timedelta(minutes=5), lease_delta=None,
               mode='react', task='test task', conn=None):
    from contextlib import nullcontext
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    with nullcontext(conn) if conn is not None else connect('AGENT_DATABASE_URL') as db:
        db.execute("INSERT INTO execution.tasks(id,kind,max_attempts,queue_deadline_at) VALUES (%s,'agent_run',2147483647,clock_timestamp()+interval '300 seconds')",(run_id,))
        db.execute("""UPDATE execution.tasks SET created_at=%s,queue_deadline_at=%s,status=%s,
            started_at=%s,execution_deadline_at=%s,execution_seconds=%s,lease_token=%s,leased_until=%s,
            attempts=%s WHERE id=%s""", (now+created_delta, now+queue_delta, status,
            now if status in {'running','stopping'} else None,
            now+timedelta(minutes=5) if status in {'running','stopping'} else None,
            300 if status in {'running','stopping'} else None,
            uuid4() if status in {'running','stopping'} else None,
            now+(lease_delta or timedelta(seconds=120)) if status in {'running','stopping'} else None,
            1 if status in {'running','stopping'} else 0, run_id))
        db.execute('INSERT INTO agent.agent_runs(id,task,mode,request_digest,graph_version) VALUES (%s,%s,%s,%s,%s)',
                   (run_id,task,mode,hashlib.sha256(run_id.bytes).digest(),GRAPH_VERSION))
    return run_id


def quiesce_runs():
    with connect('AGENT_DATABASE_URL') as conn:
        conn.execute("""UPDATE execution.tasks SET status='cancelled',finished_at=clock_timestamp(),
            termination_reason='test_cleanup',lease_token=NULL,leased_until=NULL
            WHERE kind='agent_run' AND status IN ('queued','running','stopping')""")
