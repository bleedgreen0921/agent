"""Owner-only offline policy configuration and safe operational diagnostics."""
import argparse
import json
import os
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from execution.flow_config import admission_policy, downstream_policy, number, policy_hash, validate


def configure(conn):
    validate()
    busy = conn.execute("""SELECT
        EXISTS(SELECT 1 FROM agent.worker_instances WHERE heartbeat_at>clock_timestamp()-interval '90 seconds') OR
        EXISTS(SELECT 1 FROM rag.worker_instances WHERE heartbeat_at>clock_timestamp()-interval '90 seconds') OR
        EXISTS(SELECT 1 FROM execution.tasks WHERE status IN ('running','stopping') AND leased_until>clock_timestamp()) OR
        EXISTS(SELECT 1 FROM execution.request_permits WHERE expires_at>clock_timestamp()) AS busy""").fetchone()['busy']
    if busy: raise RuntimeError('Stop API/Workers and wait for their online leases before configuring policies')
    entries = [(kind, 'request' if kind=='search' else 'submit', admission_policy(kind)) for kind in ('agent_run','rag_document','search')]
    entries.extend([('upload','request',{'rate':2,'burst':2,'unfinished':2}),
                    ('memory_job','capacity',{'rate':1,'burst':number('MEMORY_UNFINISHED_CAPACITY',32,integer=True)})])
    bindings = []
    for alias, service, env in [('agent:agent-model','agent-model','AGENT_MODEL_URL'), ('agent:rag','rag','RAG_BASE_URL'),
        ('agent:embedding','embedding','AGENT_EMBEDDING_URL' if os.environ.get('AGENT_EMBEDDING_URL') else 'RAG_EMBEDDING_URL'),
        ('rag:embedding','embedding','RAG_EMBEDDING_URL'), ('rag:rewrite','rewrite','RAG_REWRITE_URL'), ('rag:rerank','rerank','RAG_RERANK_URL')]:
        if os.environ.get(env):
            key, policy = downstream_policy(service,os.environ[env])
            entries.append(('downstream',key,policy)); bindings.append((alias,key))
    for scope,key,policy in entries:
        conn.execute('''INSERT INTO execution.flow_buckets(scope,key,policy_hash,tokens) VALUES (%s,%s,%s,%s)
            ON CONFLICT(scope,key) DO UPDATE SET policy_hash=EXCLUDED.policy_hash,
            tokens=LEAST(execution.flow_buckets.tokens,EXCLUDED.tokens),sampled_at=clock_timestamp()''',
            (scope,key,policy_hash(policy),policy['burst']))
    conn.execute('DELETE FROM execution.downstream_bindings')
    for alias,key in bindings: conn.execute('INSERT INTO execution.downstream_bindings VALUES (%s,%s)',(alias,key))


def snapshot(conn):
    conn.execute('SET TRANSACTION READ ONLY')
    return {'observed_at': conn.execute('SELECT clock_timestamp() AS at').fetchone()['at'],
        'pressure': conn.execute("SELECT scope,key,tokens,rate_factor,cooldown_until,unhealthy_since,last_error FROM execution.flow_buckets ORDER BY scope,key").fetchall(),
        'queues': conn.execute("SELECT kind,status,count(*) AS count FROM execution.tasks GROUP BY kind,status ORDER BY kind,status").fetchall(),
        'expired_activities': conn.execute("SELECT task_id,a.kind,stage,deadline_at FROM execution.task_activities a JOIN execution.tasks t ON t.id=a.task_id WHERE a.token=t.lease_token AND t.status='running' AND a.finished_at IS NULL AND a.deadline_at<=clock_timestamp() ORDER BY a.deadline_at LIMIT 100").fetchall(),
        'alerts': conn.execute('SELECT scope,key,code,first_seen_at,last_seen_at,resolved_at FROM execution.flow_alerts ORDER BY last_seen_at DESC LIMIT 100').fetchall()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure',action='store_true')
    parser.add_argument('--acknowledge',nargs=3,metavar=('SCOPE','KEY','CODE'))
    args=parser.parse_args(argv)
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL'],row_factory=dict_row) as conn:
        if args.configure:
            configure(conn)
            print('Flow policies configured; existing downstream cooldowns retained')
        elif args.acknowledge:
            conn.execute('UPDATE execution.flow_alerts SET resolved_at=clock_timestamp() WHERE scope=%s AND key=%s AND code=%s',tuple(args.acknowledge))
            print('Alert acknowledged')
        else: print(json.dumps(snapshot(conn),default=str,ensure_ascii=False))


if __name__=='__main__':main()
