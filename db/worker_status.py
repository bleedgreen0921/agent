"""Worker process heartbeats and read-only operational snapshots."""

from uuid import UUID

from db.connection import connect


TABLES = {"agent": ("AGENT_DATABASE_URL", "agent.worker_instances"),
          "rag": ("RAG_DATABASE_URL", "rag.worker_instances")}


def beat(service: str, instance_id: UUID, *, memory_child_alive: bool = False, prune: bool = False) -> None:
    env, table = TABLES[service]
    with connect(env, profile="control") as conn:
        if service == "agent":
            conn.execute(f"""INSERT INTO {table}(instance_id,memory_child_alive)
                VALUES (%s,%s) ON CONFLICT (instance_id) DO UPDATE
                SET heartbeat_at=now(),memory_child_alive=EXCLUDED.memory_child_alive""",
                (instance_id, memory_child_alive))
        else:
            conn.execute(f"""INSERT INTO {table}(instance_id) VALUES (%s)
                ON CONFLICT (instance_id) DO UPDATE SET heartbeat_at=now()""", (instance_id,))
        if prune:
            conn.execute(f"DELETE FROM {table} WHERE heartbeat_at<now()-interval '7 days'")


def remove(service: str, instance_id: UUID) -> None:
    env, table = TABLES[service]
    with connect(env, profile="control") as conn:
        conn.execute(f"DELETE FROM {table} WHERE instance_id=%s", (instance_id,))


def snapshot(service: str) -> dict:
    env, table = TABLES[service]
    with connect(env) as conn:
        conn.read_only = True
        summary = conn.execute(f"""SELECT now() AS observed_at,
            count(*) FILTER (WHERE heartbeat_at>now()-interval '90 seconds') AS online_count,
            max(heartbeat_at) AS last_heartbeat_at FROM {table}""").fetchone()
        extra = ",memory_child_alive" if service == "agent" else ""
        instances = conn.execute(f"""SELECT instance_id,started_at,heartbeat_at,
            heartbeat_at>now()-interval '90 seconds' AS online{extra}
            FROM {table} ORDER BY heartbeat_at DESC LIMIT 100""").fetchall()
        if service == "agent":
            queue = conn.execute("""SELECT
                count(*) FILTER (WHERE status='queued' AND queue_deadline_at>now()) AS queued_count,
                min(created_at) FILTER (WHERE status='queued' AND queue_deadline_at>now()) AS oldest_queued_at,
                count(*) FILTER (WHERE status='queued' AND queue_deadline_at<=now()) AS overdue_count,
                count(*) FILTER (WHERE status IN ('running','cancelling') AND leased_until<=now()) AS expired_lease_count
                FROM agent.agent_runs""").fetchone()
        else:
            queue = conn.execute("""SELECT
                count(*) FILTER (WHERE status IN ('queued','retry') AND available_at<=now()) AS queued_count,
                min(available_at) FILTER (WHERE status IN ('queued','retry') AND available_at<=now()) AS oldest_queued_at,
                count(*) FILTER (WHERE status IN ('queued','retry','running') AND deadline_at<=now()) AS overdue_count,
                count(*) FILTER (WHERE status='running' AND leased_until<=now()) AS expired_lease_count
                FROM rag.processing_jobs""").fetchone()
    return {"service": service, **summary, **queue, "instances": [dict(row) for row in instances]}
