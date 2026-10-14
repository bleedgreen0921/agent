"""Immutable input capture and Memory enqueue live in the execution layer."""
from uuid import uuid4
from psycopg.types.json import Jsonb
from execution.contracts import TaskRef, TaskKind
from execution import store
from execution.retry import retry_transaction

async def enqueue_memory(conn, turn_id, kind):
    if await (await conn.execute('SELECT id FROM agent.memory_jobs WHERE kind=%s AND turn_id=%s', (kind, turn_id))).fetchone():
        return
    job_id = uuid4()
    source = await (await conn.execute('SELECT run_id FROM agent.conversation_turns WHERE id=%s', (turn_id,))).fetchone()
    await store.create(conn, TaskRef(job_id, TaskKind.MEMORY), reservation_run_id=source['run_id'] if source else None)
    await conn.execute('INSERT INTO agent.memory_jobs(id,kind,turn_id) VALUES (%s,%s,%s)', (job_id, kind, turn_id))

async def capture(conn, task, token, table, snapshot):
    if table not in {'run_experiment_snapshots', 'run_memory_snapshots'}:
        raise ValueError('Unknown immutable input table')
    await store.assert_active(conn, task, token)
    if table == 'run_experiment_snapshots':
        await conn.execute('INSERT INTO agent.run_experiment_snapshots(run_id,schema_version,snapshot) VALUES (%s,1,%s) ON CONFLICT(run_id) DO NOTHING', (task.id, Jsonb(snapshot)))
    else:
        await conn.execute('INSERT INTO agent.run_memory_snapshots(run_id,snapshot) VALUES (%s,%s) ON CONFLICT(run_id) DO NOTHING', (task.id, Jsonb(snapshot)))
    snapshot = (await (await conn.execute(f'SELECT snapshot FROM agent.{table} WHERE run_id=%s', (task.id,))).fetchone())['snapshot']
    await store.assert_active(conn, task, token)
    return snapshot

def capture_experiment_data(settings, version):
    import hashlib
    from experiment_service.catalogue import register, canonical
    from agent_service.tooling import RecoverableToolError
    try:
        entries = register(settings.root)
        projects = {entry['config']['run']['project_id'] for entry in entries}
        if len(projects) != 1 or not all((isinstance(v, str) and v for v in projects)):
            raise ValueError('Root must contain exactly one research project')
        snapshot = {'schema_version': 1, 'method_version': version, 'root_sha256': settings.root_sha256, 'project_id': projects.pop(), 'snapshot_sha256': hashlib.sha256(canonical(entries).encode()).hexdigest(), 'entries': entries}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise RecoverableToolError('EXPERIMENT_SOURCE_INVALID', f'Experiment source validation failed ({type(exc).__name__})') from exc
    return snapshot

async def experiment_snapshot(task, token, settings, version):
    import hashlib
    from execution.persistence import transaction as connect
    from experiment_service.catalogue import register, canonical
    from agent_service.tooling import RecoverableToolError
    existing = await _experiment_snapshot_read(task, token)
    if existing:
        snapshot = existing['snapshot']
    else:
        from execution.persistence import resources, deadline
        from execution.scope import work_owner, authorize
        await authorize()
        snapshot = await resources().cpu.run(capture_experiment_data,settings,version,owner=work_owner(),deadline=deadline(300),before_submit=authorize)
        snapshot = await _experiment_snapshot_publish(task, token, snapshot)
    if snapshot['method_version'] != version or snapshot['root_sha256'] != settings.root_sha256:
        raise RecoverableToolError('EXPERIMENT_SNAPSHOT_INCOMPATIBLE', 'Run requires its original experiment root and tool version')
    return snapshot


@retry_transaction(profile='control')
async def _experiment_snapshot_read(task, token):
    from execution.persistence import transaction
    async with transaction('AGENT_DATABASE_URL', profile='control') as conn:
        await store.assert_active(conn, task, token)
        return await (await conn.execute('SELECT snapshot FROM agent.run_experiment_snapshots WHERE run_id=%s', (task.id,))).fetchone()


@retry_transaction(profile='control')
async def _experiment_snapshot_publish(task, token, snapshot):
    from execution.persistence import transaction
    async with transaction('AGENT_DATABASE_URL', profile='control') as conn:
        return await capture(conn, task, token, 'run_experiment_snapshots', snapshot)

async def prepare_inputs(task, token):
    from agent_service.tooling import load_providers
    from execution.contracts import freeze
    from execution.persistence import resources, deadline
    from execution.scope import work_owner, authorize
    runtime = resources()
    if getattr(runtime, 'agent_settings', None) is None and getattr(runtime, 'tool_providers', None) is None:
        runtime.tool_providers = await runtime.io.run(load_providers, owner=work_owner(), deadline=deadline(30), before_submit=authorize)
    inputs = {}
    providers = runtime.agent_settings['providers'] if getattr(runtime, 'agent_settings', None) else runtime.tool_providers
    if any(provider.name == 'experiment_analysis' for _, provider in providers):
        from agent_service.tools.experiments import settings_from_env, VERSION
        from execution.persistence import resources, deadline
        from execution.scope import work_owner, authorize
        runtime = resources()
        settings = getattr(runtime, 'agent_settings', {}).get('experiment')
        if settings is None:
            settings = await runtime.io.run(settings_from_env, owner=work_owner(), deadline=deadline(30), before_submit=authorize)
        inputs['experiment'] = await experiment_snapshot(task, token, settings, VERSION)
    if inputs:
        from execution.persistence import resources, deadline
        from execution.scope import work_owner
        return await resources().io.run(freeze, inputs, owner=work_owner(), deadline=deadline(30))
    return freeze(inputs)
