"""Prepare immutable provider/configuration metadata before claiming task locks."""
import os
from agent_service.manifest import execution_manifest
from agent_service.tooling import load_providers
from execution.infra.types import OperationDeadline
from execution.retry import retry_transaction


def prepare():
    providers = load_providers()
    manifest = execution_manifest(providers=providers)
    from agent_service.tools.experiments import settings_from_env
    experiment = settings_from_env() if any(provider.name == 'experiment_analysis' for _, provider in providers) else None
    return {'experiment': experiment, 'providers': providers, 'manifest': manifest, 'model': manifest['model'],
            'url': os.environ.get('AGENT_MODEL_URL', ''), 'key': os.environ.get('AGENT_MODEL_KEY', 'local')}


async def initialize(runtime):
    from execution.tokens import configuration
    runtime.token_settings=await runtime.io.run(configuration,deadline=OperationDeadline.after(30))
    await runtime.http.register_available()
    runtime.agent_settings = await runtime.io.run(prepare, deadline=OperationDeadline.after(30))
    from agent_service.model_adapter import model
    model()  # Validate/create only native async clients; no request is sent.


def compatible(saved, current):
    from agent_service.versions import RECOVERY_CONTRACT_VERSION
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return False
    contract = saved.get('recovery_contract')
    if not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != RECOVERY_CONTRACT_VERSION:
        return False
    if not {'version','state_schema_version','sha256','legacy_policy','state_migration'} <= contract.keys():
        return False
    if not isinstance(contract.get('sha256'), str) or len(contract['sha256']) != 64 or any(c not in '0123456789abcdef' for c in contract['sha256']):
        return False
    return all(key in saved and key in current and saved[key] == current[key]
               for key in ('agent', 'model', 'tools', 'recovery_contract'))


def execution_contract_now(settings):
    if settings is None:
        return execution_manifest()
    # Keep the actual frozen model/provider configuration, but recheck source,
    # schemas and dependencies. A stale startup cache cannot bless a changed
    # implementation during a later direct invocation or interrupted recovery.
    from agent_service.manifest import recovery_contract
    return {**settings['manifest'], 'recovery_contract': recovery_contract(settings['providers'])}


async def validate_execution(run_id, token):
    """Check before inputs, models or graph decoding, including direct runners."""
    from execution.persistence import resources, deadline
    from execution.scope import work_owner
    runtime = resources()
    settings = getattr(runtime, 'agent_settings', None)
    current = await runtime.io.run(execution_contract_now, settings, owner=work_owner(), deadline=deadline(30))
    return await _validate_execution(run_id, token, current)


@retry_transaction(profile='control')
async def _validate_execution(run_id, token, current):
    from execution.persistence import transaction
    from execution.contracts import TaskRef, TaskKind
    from execution import store
    from execution.errors import ExecutionRevoked
    from agent_service.versions import GRAPH_VERSION, MANIFEST_SCHEMA_VERSION
    async with transaction('AGENT_DATABASE_URL', profile='control') as conn:
        await store.assert_active(conn, TaskRef(run_id, TaskKind.AGENT), token)
        row = await (await conn.execute('''SELECT r.graph_version,r.recovery_protocol,m.schema_version,m.manifest
            FROM agent.agent_runs r LEFT JOIN agent.run_manifests m ON m.run_id=r.id WHERE r.id=%s''', (run_id,))).fetchone()
        valid = bool(row and row['graph_version'] == GRAPH_VERSION and row['recovery_protocol'] == 2
                     and row['schema_version'] == MANIFEST_SCHEMA_VERSION and compatible(row['manifest'], current))
        if not valid:
            from agent_service.storage.runtime import interrupt_calls
            result = await store.settle_failure(conn, TaskRef(run_id, TaskKind.AGENT), token,
                                               'INTERRUPTED_UNKNOWN', reason='configuration_mismatch')
            if result.accepted:
                await interrupt_calls(conn, run_id)
        else:
            await store.assert_active(conn, TaskRef(run_id, TaskKind.AGENT), token)
    if not valid:
        raise ExecutionRevoked('Execution manifest is incompatible with this runtime')
