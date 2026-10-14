"""Narrow owner-defined lifecycle operations, never business-role deletion."""
from alembic import op
revision='execution_0004'
down_revision='execution_0003'
branch_labels=depends_on=None


def upgrade():
    op.execute('''CREATE TABLE execution.retention_holds(namespace text NOT NULL,id text NOT NULL,
        reason text NOT NULL, PRIMARY KEY(namespace,id))''')
    op.execute('''CREATE TABLE execution.lifecycle_audit(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        action text NOT NULL, entity text NOT NULL, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp())''')
    op.execute('''CREATE FUNCTION execution.gc_candidates() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,execution AS $$
        BEGIN RETURN jsonb_build_object(
          'runs',(SELECT coalesce(jsonb_agg(id),'[]') FROM execution.tasks t WHERE kind='agent_run'
            AND status IN ('completed','partial','failed','cancelled') AND finished_at<clock_timestamp()-interval '30 days'
            AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace='agent' AND h.id=t.id::text)),
          'artifacts',(SELECT coalesce(jsonb_agg(to_jsonb(a)),'[]') FROM execution.artifacts a WHERE NOT hold AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace='rag' AND h.id=a.id::text)
            AND writer_until<clock_timestamp() AND updated_at<clock_timestamp()-interval '24 hours'
            AND (state<>'deleted' OR quarantine_path IS NOT NULL)),
          'operations',(SELECT coalesce(jsonb_agg(id),'[]') FROM rag.search_operations s WHERE expires_at<clock_timestamp() AND status<>'expired'
            AND NOT EXISTS(SELECT 1 FROM execution.tasks t WHERE t.id::text=s.run_id AND t.status NOT IN ('completed','partial','failed','cancelled'))),
          'registered_paths',(SELECT coalesce(jsonb_agg(path),'[]') FROM (
            SELECT file_path AS path FROM rag.document_versions UNION SELECT path FROM execution.artifacts
            UNION SELECT target_path FROM execution.artifacts WHERE target_path IS NOT NULL) p),
          'archives',(SELECT coalesce(jsonb_agg(to_jsonb(a)),'[]') FROM execution.call_archives a
            WHERE expires_at<clock_timestamp() AND NOT EXISTS(SELECT 1 FROM execution.tasks t WHERE (t.id=a.task_id OR t.id::text=a.run_reference) AND t.status NOT IN ('completed','partial','failed','cancelled')) AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace IN (a.namespace,'agent') AND h.id IN (coalesce(a.task_id::text,a.run_reference,a.id::text),a.id::text))));
        END $$''')
    op.execute('''CREATE FUNCTION execution.gc_run(target uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,execution AS $$
        DECLARE valid boolean;
        BEGIN
          SELECT true INTO valid FROM execution.tasks t WHERE id=target AND kind='agent_run'
            AND status IN ('completed','partial','failed','cancelled') AND finished_at<clock_timestamp()-interval '30 days'
            AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace='agent' AND h.id=t.id::text)
            FOR UPDATE;
          IF valid IS DISTINCT FROM true THEN RETURN false; END IF;
          DELETE FROM agent.call_checkpoint_receipts WHERE run_id=target;
          DELETE FROM agent.checkpoint_writes WHERE thread_id=target::text OR thread_id LIKE target::text||':step:%';
          DELETE FROM agent.checkpoints WHERE thread_id=target::text OR thread_id LIKE target::text||':step:%';
          DELETE FROM agent.checkpoint_blobs WHERE thread_id=target::text OR thread_id LIKE target::text||':step:%';
          DELETE FROM agent.body_pins WHERE run_id=target AND owner='recovery';
          DELETE FROM agent.execution_bodies b WHERE run_id=target AND NOT EXISTS(
            SELECT 1 FROM agent.body_pins p WHERE p.run_id=b.run_id AND p.sha256=b.sha256);
          INSERT INTO execution.lifecycle_audit(action,entity) VALUES('checkpoint_gc',target::text);
          RETURN true;
        END $$''')
    op.execute('''CREATE FUNCTION execution.gc_artifact(target uuid, phase text, location text DEFAULT NULL)
        RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,execution AS $$
        DECLARE a execution.artifacts%ROWTYPE;
        BEGIN
          SELECT * INTO a FROM execution.artifacts WHERE namespace='rag' AND id=target FOR UPDATE;
          IF NOT FOUND OR a.hold OR EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace='rag' AND h.id=a.id::text) OR a.writer_until>=clock_timestamp() OR a.updated_at>=clock_timestamp()-interval '24 hours'
             OR EXISTS(SELECT 1 FROM rag.document_versions WHERE file_path=a.target_path OR file_path=a.path)
          THEN RETURN false; END IF;
          IF phase='mark' AND a.state IN ('writing','prepared','unknown','referenced') THEN
            UPDATE execution.artifacts SET state='quarantined',quarantine_path=location,quarantined_at=clock_timestamp() WHERE namespace='rag' AND id=target;
          ELSIF phase='delete' AND a.state='quarantined' AND a.quarantined_at<clock_timestamp()-interval '24 hours' THEN
            UPDATE execution.artifacts SET state='deleted',updated_at=clock_timestamp() WHERE namespace='rag' AND id=target;
          ELSIF phase='purge' AND a.state='deleted' THEN
            UPDATE execution.artifacts SET quarantine_path=NULL WHERE namespace='rag' AND id=target;
          ELSE RETURN false; END IF;
          INSERT INTO execution.lifecycle_audit(action,entity) VALUES('artifact_'||phase,target::text);
          RETURN true;
        END $$''')
    op.execute('''CREATE FUNCTION execution.gc_archive(ns text,target uuid,phase text DEFAULT 'mark') RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,execution AS $$ BEGIN
        IF phase='mark' THEN
          UPDATE execution.call_archives a SET status='gc_marked' WHERE namespace=ns AND id=target AND expires_at<clock_timestamp()
            AND NOT EXISTS(SELECT 1 FROM execution.tasks t WHERE (t.id=a.task_id OR t.id::text=a.run_reference) AND t.status NOT IN ('completed','partial','failed','cancelled'))
            AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.namespace IN (a.namespace,'agent') AND h.id IN (coalesce(a.task_id::text,a.run_reference,a.id::text),a.id::text));
        ELSIF phase='delete' THEN
          DELETE FROM execution.call_archives a WHERE namespace=ns AND id=target AND status='gc_marked';
        ELSE RETURN false; END IF;
        IF NOT FOUND THEN RETURN false; END IF;
        INSERT INTO execution.lifecycle_audit(action,entity) VALUES('archive_gc',ns||':'||target::text); RETURN true; END $$''')
    op.execute('''CREATE FUNCTION execution.gc_search(target uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,execution AS $$ BEGIN
        UPDATE rag.search_operations s SET status='expired',result=NULL WHERE id=target AND expires_at<clock_timestamp()
            AND NOT EXISTS(SELECT 1 FROM execution.tasks t WHERE t.id::text=s.run_id AND t.status NOT IN ('completed','partial','failed','cancelled'))
            AND NOT EXISTS(SELECT 1 FROM execution.retention_holds h WHERE h.id IN (s.run_id,s.id::text));
        RETURN FOUND; END $$''')
    for name in ('gc_candidates()','gc_run(uuid)','gc_artifact(uuid,text,text)','gc_archive(text,uuid,text)','gc_search(uuid)'):
        op.execute(f'REVOKE ALL ON FUNCTION execution.{name} FROM PUBLIC')


def downgrade():raise RuntimeError('Restore matching code and database backup')
