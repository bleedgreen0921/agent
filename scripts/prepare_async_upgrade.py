"""Prepare drained protocol-1 Agent tasks before upgrading a personal database.

Run only after stopping old API/Workers and backing up the database. No migrations,
process termination, or historical result/checkpoint rewrites occur here.
"""
import os
import psycopg
from psycopg.rows import dict_row
from db.migrate import validate_database


def main():
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL'],row_factory=dict_row) as conn:
        # Validation expects tuple rows; perform it on a separate read-only probe.
        with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as probe:validate_database(probe)
        for table in ('agent.worker_instances','rag.worker_instances'):
            if conn.execute(f"SELECT 1 FROM {table} WHERE heartbeat_at>clock_timestamp()-interval '90 seconds' LIMIT 1").fetchone():
                raise RuntimeError('Stop old Workers and wait for their heartbeat records to expire before preparation')
        version=conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='agent' AND table_name='agent_runs' AND column_name='recovery_protocol'").fetchone()
        predicate="AND r.recovery_protocol=1" if version else ''
        rows=conn.execute(f"""SELECT t.* FROM execution.tasks t JOIN agent.agent_runs r ON r.id=t.id
            WHERE t.kind='agent_run' AND t.started_at IS NOT NULL AND t.status IN ('queued','running','stopping') {predicate}
            ORDER BY t.id FOR UPDATE OF t""").fetchall()
        for row in rows:
            now=conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            status,code,reason=('cancelled',None,'cancelled') if row['stop_requested_at'] else ('failed','RUN_TIMEOUT','deadline') if row['execution_deadline_at']<=now else ('failed','INTERRUPTED_UNKNOWN','interrupted_unknown')
            conn.execute('''UPDATE execution.tasks SET status=%s,error_code=%s,termination_reason=%s,
                finished_at=clock_timestamp(),updated_at=clock_timestamp(),lease_token=NULL,leased_until=NULL,worker_id=NULL
                WHERE id=%s AND kind='agent_run' ''',(status,code,reason,row['id']))
            for table in ('model_calls','tool_calls'):
                conn.execute(f"UPDATE agent.{table} SET status='interrupted_unknown',error_code='INTERRUPTED_UNKNOWN',finished_at=clock_timestamp() WHERE run_id=%s AND status='started'",(row['id'],))
            conn.execute("UPDATE agent.run_steps SET status='failed',error_code=%s,finished_at=clock_timestamp() WHERE run_id=%s AND status='running'",(code or 'CANCELLED',row['id']))
    print(f'Prepared {len(rows)} interrupted historical Agent tasks; history retained')


if __name__=='__main__':main()
