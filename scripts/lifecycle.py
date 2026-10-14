"""Local diagnostic inspection and repeatable, bounded lifecycle maintenance."""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import psycopg
from psycopg.rows import dict_row


def safe_path(value,root):
    path=Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve().parent!=root.resolve():
        raise ValueError('Lifecycle path is outside its managed directory')
    return path


def collect(conn,*,dry_run=False):
    items=conn.execute('SELECT execution.gc_candidates() AS data').fetchone()['data']
    files=Path(os.environ.get('RAG_FILES_DIR','.data/personal-rag')).resolve()
    registered=set(items.pop('registered_paths'))
    items['unregistered_files']=[str(p) for p in files.iterdir() if p.is_file() and not p.name.endswith('.lock') and str(p) not in registered] if files.is_dir() else []
    if dry_run:return items
    archive=Path(os.environ.get('CALL_ARCHIVE_DIR','.data/call-archives')).resolve()
    for run_id in items['runs']:
        conn.execute('SELECT execution.gc_run(%s)',(run_id,))
    for operation in items['operations']:conn.execute('SELECT execution.gc_search(%s)',(operation,))
    for item in items['artifacts']:
        if item['namespace']!='rag':continue
        paths=[safe_path(p,files) for p in (item['path'],item['target_path']) if p]
        if item['state']=='deleted':
            quarantine=files/'.quarantine'/item['id']
            if quarantine.parent.is_symlink() or quarantine.is_symlink():raise ValueError('Quarantine must not contain symbolic links')
            if item['quarantine_path']!=str(quarantine):raise ValueError('Unexpected quarantine directory')
            if quarantine.exists():shutil.rmtree(quarantine)
            conn.execute("SELECT execution.gc_artifact(%s,'purge',NULL)",(item['id'],))
            continue
        lock_path=paths[0].with_name(paths[0].name+'.lock')
        if lock_path.is_symlink():continue
        lock=os.fdopen(os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600),'a+b')
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:continue
            quarantine=files/'.quarantine'/item['id']
            if quarantine.parent.is_symlink() or quarantine.is_symlink():raise ValueError('Quarantine must not contain symbolic links')
            if item['state']!='quarantined':
                allowed=conn.execute("SELECT execution.gc_artifact(%s,'mark',%s) AS ok",(item['id'],str(quarantine))).fetchone()['ok']
                if not allowed:continue
            else:
                if Path(item['quarantine_path'])!=quarantine:raise ValueError('Unexpected quarantine directory')
            quarantine.mkdir(parents=True,exist_ok=True,mode=0o700)
            # Retry a crash between database marking and the actual rename.
            for path in paths:
                if path.exists():os.replace(path,quarantine/path.name)
            if item['state']=='quarantined':
                allowed=conn.execute("SELECT execution.gc_artifact(%s,'delete',NULL) AS ok",(item['id'],)).fetchone()['ok']
                if allowed:
                    shutil.rmtree(quarantine)
                    lock_path.unlink(missing_ok=True)
                    conn.execute("SELECT execution.gc_artifact(%s,'purge',NULL)",(item['id'],))
        finally:lock.close()
    for item in items['archives']:
        # A crash after output rename but before its DB update can leave the
        # deterministic output name, or an interrupted atomic-write temporary.
        output=item['output_path'] or item['input_path'].replace('-input.json.gz','-output.json.gz')
        paths=[safe_path(p,archive) for p in (item['input_path'],output) if p]
        temporaries=[safe_path(str(p),archive) for path in paths for p in archive.glob(path.name+'.*.tmp')]
        if any(p.stat().st_mtime>=time.time()-86400 for p in temporaries):continue
        paths+=temporaries
        # Claim metadata first: the restricted function rechecks retention holds.
        if conn.execute('SELECT execution.gc_archive(%s,%s) AS ok',(item['namespace'],item['id'])).fetchone()['ok']:
            for path in paths:path.unlink(missing_ok=True)
            conn.execute("SELECT execution.gc_archive(%s,%s,'delete')",(item['namespace'],item['id']))
    return {'runs':len(items['runs']),'artifacts':len(items['artifacts']),'archives':len(items['archives'])}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('inspect','dry-run','run-once','daemon','hold','release','archive'))
    parser.add_argument('--namespace',choices=('agent','rag'),default='agent')
    parser.add_argument('--id');parser.add_argument('--reason',default='Manual retention')
    args=parser.parse_args(argv)
    with psycopg.connect(os.environ['MAINTENANCE_DATABASE_URL'],autocommit=True,row_factory=dict_row,
                         options='-c statement_timeout=5000 -c lock_timeout=1000') as conn:
        if args.command in ('hold','release'):
            if not args.id:parser.error('--id required')
            if args.command=='hold':conn.execute('INSERT INTO execution.retention_holds(namespace,id,reason) VALUES (%s,%s,%s) ON CONFLICT(namespace,id) DO UPDATE SET reason=excluded.reason',(args.namespace,args.id,args.reason))
            else:conn.execute('DELETE FROM execution.retention_holds WHERE namespace=%s AND id=%s',(args.namespace,args.id))
            return
        if args.command=='archive':
            if not args.id:parser.error('--id required')
            rows=conn.execute('SELECT * FROM execution.call_archives WHERE namespace=%s AND (id::text=%s OR call_id::text=%s OR task_id::text=%s OR run_reference=%s)',(args.namespace,args.id,args.id,args.id,args.id)).fetchall()
            for row in rows:
                for key in ('input','output'):
                    if row[key+'_path']:
                        path=safe_path(row[key+'_path'],Path(os.environ.get('CALL_ARCHIVE_DIR','.data/call-archives')))
                        raw=gzip.decompress(path.read_bytes())
                        if hashlib.sha256(raw).hexdigest()!=row[key+'_sha256']:raise ValueError('Archive checksum mismatch')
                        print(json.dumps({'archive_id':str(row['id']),'part':key,'body':json.loads(raw)},ensure_ascii=False))
            return
        while True:
            print(json.dumps(collect(conn,dry_run=args.command in ('inspect','dry-run')),default=str,ensure_ascii=False),flush=True)
            if args.command!='daemon':break
            time.sleep(900)


if __name__=='__main__':main()
