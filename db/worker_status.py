"""Worker process heartbeats and read-only operational snapshots."""
from uuid import UUID
from execution.persistence import transaction as connect
from db.connection import connect as offline_connect
TABLES = {'agent': ('AGENT_DATABASE_URL', 'agent.worker_instances'), 'rag': ('RAG_DATABASE_URL', 'rag.worker_instances')}

async def beat(service: str, instance_id: UUID, *, memory_consumer_alive: bool=False, prune: bool=False, resource_state=None) -> None:
    env, table = TABLES[service]
    async with connect(env, profile='control') as conn:
        if service == 'agent':
            await conn.execute(f'INSERT INTO {table}(instance_id,memory_consumer_alive,started_at,heartbeat_at)\n                VALUES (%s,%s,clock_timestamp(),clock_timestamp()) ON CONFLICT (instance_id) DO UPDATE\n                SET heartbeat_at=clock_timestamp(),memory_consumer_alive=EXCLUDED.memory_consumer_alive', (instance_id, memory_consumer_alive))
        else:
            await conn.execute(f'INSERT INTO {table}(instance_id,started_at,heartbeat_at) VALUES (%s,clock_timestamp(),clock_timestamp())\n                ON CONFLICT (instance_id) DO UPDATE SET heartbeat_at=clock_timestamp()', (instance_id,))
        if resource_state is not None:
            from psycopg.types.json import Jsonb
            kind = 'agent_run' if service == 'agent' else 'rag_document'
            await conn.execute("""INSERT INTO execution.worker_resources(instance_id,kind,state)
                VALUES (%s,%s,%s) ON CONFLICT(instance_id) DO UPDATE
                SET observed_at=clock_timestamp(),state=EXCLUDED.state""", (instance_id,kind,Jsonb(resource_state)))
        if prune:
            await conn.execute(f"DELETE FROM {table} WHERE heartbeat_at<statement_timestamp()-interval '7 days'")

async def remove(service: str, instance_id: UUID) -> None:
    env, table = TABLES[service]
    async with connect(env, profile='control') as conn:
        await conn.execute(f'DELETE FROM {table} WHERE instance_id=%s', (instance_id,))
        await conn.execute('DELETE FROM execution.worker_resources WHERE instance_id=%s', (instance_id,))

def snapshot(service: str) -> dict:
    env, table = TABLES[service]
    with offline_connect(env) as conn:
        conn.read_only = True
        observed = conn.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        summary_sql, instances_sql, queue_sql = snapshot_queries(service)
        summary = conn.execute(summary_sql, (observed,)).fetchone()
        instances = conn.execute(instances_sql, (observed,)).fetchall()
        queue = conn.execute(queue_sql, (observed,)).fetchone()
        classes=conn.execute("""SELECT kind,count(*) FILTER(WHERE status='queued' AND started_at IS NULL) AS new_queued,
            count(*) FILTER(WHERE status='queued' AND started_at IS NOT NULL) AS recovery_queued,
            count(*) FILTER(WHERE status IN ('running','stopping')) AS executing FROM execution.tasks GROUP BY kind""").fetchall()
        pressure=conn.execute("SELECT key AS group_id,cooldown_until,unhealthy_since,rate_factor,last_error FROM execution.flow_buckets WHERE scope='downstream' ORDER BY key").fetchall()
        alerts=conn.execute('SELECT scope,key,code,first_seen_at,last_seen_at FROM execution.flow_alerts WHERE resolved_at IS NULL ORDER BY last_seen_at DESC LIMIT 100').fetchall()
        states={r['instance_id']:r['state'] for r in conn.execute('SELECT instance_id,state FROM execution.worker_resources').fetchall()}
        for instance in instances:
            if instance['instance_id'] in states: instance['resources']=states[instance['instance_id']]
    return {'service': service, **summary, **queue, 'instances': [dict(row) for row in instances], 'work_classes':classes, 'downstream':pressure, 'alerts':alerts}


def snapshot_queries(service: str) -> tuple[str, str, str]:
    """All report queries share one freshly sampled observation cutoff."""
    _, table = TABLES[service]
    clock = 'WITH clock AS (SELECT %s::timestamptz AS at) '
    summary = clock + f"""SELECT (SELECT at FROM clock) AS observed_at,
        count(*) FILTER (WHERE heartbeat_at>clock.at-interval '90 seconds') AS online_count,
        max(heartbeat_at) AS last_heartbeat_at FROM {table} CROSS JOIN clock"""
    extra = ',memory_consumer_alive' if service == 'agent' else ''
    instances = clock + f"""SELECT instance_id,started_at,heartbeat_at,
        heartbeat_at>clock.at-interval '90 seconds' AS online{extra}
        FROM {table} CROSS JOIN clock ORDER BY heartbeat_at DESC LIMIT 100"""
    if service == 'agent':
        queue = clock + """SELECT
            count(*) FILTER (WHERE status='queued' AND queue_deadline_at>clock.at) AS queued_count,
            min(created_at) FILTER (WHERE status='queued' AND queue_deadline_at>clock.at) AS oldest_queued_at,
            count(*) FILTER (WHERE status='queued' AND queue_deadline_at<=clock.at) AS overdue_count,
            count(*) FILTER (WHERE status IN ('running','cancelling') AND leased_until<=clock.at) AS expired_lease_count
            FROM agent.run_state CROSS JOIN clock"""
    else:
        queue = clock + """SELECT
            count(*) FILTER (WHERE status IN ('queued','retry') AND available_at<=clock.at) AS queued_count,
            min(available_at) FILTER (WHERE status IN ('queued','retry') AND available_at<=clock.at) AS oldest_queued_at,
            count(*) FILTER (WHERE status IN ('queued','retry','running') AND deadline_at<=clock.at) AS overdue_count,
            count(*) FILTER (WHERE status='running' AND leased_until<=clock.at) AS expired_lease_count
            FROM rag.processing_job_state CROSS JOIN clock"""
    return summary, instances, queue
