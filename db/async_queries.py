"""Only readiness and Worker snapshots migrate in infrastructure phase two."""
import os
import asyncio
import psycopg
from execution.infra.types import OperationDeadline, ResourceError, ResourceClosed
from db.readiness import AGENT_TABLES, RAG_TABLES
from db.worker_status import snapshot_queries


async def ready(resources, service):
    if service == 'rag' and len(os.environ.get('RAG_SERVICE_TOKEN', '')) < 43:
        return False
    try:
        async with resources.db.transaction('control', OperationDeadline.after(2), readonly=True) as conn:
            for table in AGENT_TABLES if service == 'agent' else RAG_TABLES:
                await conn.execute(f'SELECT 1 FROM {table} LIMIT 0')
            if service == 'rag':
                cursor = await conn.execute("""SELECT 1 FROM rag.index_state s
                    JOIN rag.index_revisions r ON r.id=s.active_revision_id
                    WHERE s.singleton=true AND r.state='active' LIMIT 1""")
                return await cursor.fetchone() is not None
        return True
    except asyncio.CancelledError:
        if resources.accepting:
            raise
        return False
    except (ResourceError, psycopg.Error, OSError, TimeoutError):
        return False


async def worker_snapshot(resources, service):
    try:
        return await _worker_snapshot(resources, service)
    except asyncio.CancelledError:
        if resources.accepting:
            raise
        raise ResourceClosed('Worker query interrupted by resource shutdown') from None


async def _worker_snapshot(resources, service):
    async with resources.db.transaction('normal', OperationDeadline.after(5), readonly=True) as conn:
        observed = (await (await conn.execute('SELECT clock_timestamp() AS at')).fetchone())['at']
        summary_sql, instances_sql, queue_sql = snapshot_queries(service)
        cursor = await conn.execute(summary_sql, (observed,))
        summary = await cursor.fetchone()
        cursor = await conn.execute(instances_sql, (observed,))
        instances = await cursor.fetchall()
        cursor = await conn.execute(queue_sql, (observed,))
        queue = await cursor.fetchone()
        classes = await (await conn.execute("""SELECT kind,
            count(*) FILTER(WHERE status='queued' AND started_at IS NULL) AS new_queued,
            count(*) FILTER(WHERE status='queued' AND started_at IS NOT NULL) AS recovery_queued,
            count(*) FILTER(WHERE status IN ('running','stopping')) AS executing
            FROM execution.tasks GROUP BY kind""")).fetchall()
        from execution.flow import pressure_snapshot
        pressure = await pressure_snapshot(conn)
        alerts = await (await conn.execute('SELECT scope,key,code,first_seen_at,last_seen_at FROM execution.flow_alerts WHERE resolved_at IS NULL ORDER BY last_seen_at DESC LIMIT 100')).fetchall()
        observations = await (await conn.execute('SELECT instance_id,observed_at,state FROM execution.worker_resources')).fetchall()
        states = {row['instance_id']: row for row in observations}
        for instance in instances:
            if instance['instance_id'] in states:
                instance['resources'] = states[instance['instance_id']]['state']
    from execution.admission import AdmissionStore
    from execution.persistence import using
    with using(resources):
        accepting = await AdmissionStore().status('agent_run' if service == 'agent' else 'rag_document')
    return {'service': service, **summary, **queue, 'instances': [dict(row) for row in instances], 'work_classes': classes,
            'downstream': pressure, 'alerts': alerts, 'admission': accepting}
