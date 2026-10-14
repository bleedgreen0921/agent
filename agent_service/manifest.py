"""Build the immutable execution manifest captured when a Run is claimed."""

import hashlib
import json
import inspect
from importlib.metadata import version
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from agent_service.versions import GRAPH_VERSION, MANIFEST_SCHEMA_VERSION, PROMPT_SET_VERSION, STATE_SCHEMA_VERSION, RECOVERY_CONTRACT_VERSION
from agent_service.timeouts import configured_timeouts


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def recovery_contract(providers) -> dict:
    """Fail closed on implementation/schema drift even if a version was not bumped."""
    paths = sorted(str(path.relative_to(REPOSITORY_ROOT))
                   for package in ('agent_service', 'execution', 'experiment_service', 'contracts')
                   for path in (REPOSITORY_ROOT / package).rglob('*.py') if 'migrations' not in path.relative_to(REPOSITORY_ROOT).parts)
    if not paths:
        raise RuntimeError('Recovery manifest requires executable source artifacts')
    implementation = {path: hashlib.sha256((REPOSITORY_ROOT / path).read_bytes()).hexdigest() for path in paths}
    tools = []
    for factory, provider in providers:
        for declaration in provider.tools:
            tool = declaration.tool
            schema = tool.tool_call_schema
            if not isinstance(schema, dict):
                schema = schema.model_json_schema()
            implementation_fn = getattr(tool, 'coroutine', None) or type(tool)._arun
            source = inspect.getsourcefile(implementation_fn)
            source_digest = hashlib.sha256(Path(source).read_bytes()).hexdigest() if source and Path(source).is_file() else None
            # A provider without inspectable code must deliberately version its
            # implementation; its declared versions and schema remain compared.
            tools.append({'factory': factory, 'name': tool.name, 'schema': schema,
                          'description': tool.description, 'implementation_sha256': source_digest})
    from agent_service.schemas import Plan, FinalDraft, GoalContract, StructuredSummary, StepEvaluation, ToolBatchEvaluation, FinalEvaluation
    from contracts.v1 import Evidence
    payload = {'implementation': implementation, 'tools': tools,
               'schemas': {s.__name__: s.model_json_schema() for s in (Plan, FinalDraft, Evidence, GoalContract, StructuredSummary, StepEvaluation, ToolBatchEvaluation, FinalEvaluation)},
               'dependencies': {name: version(name) for name in ('langgraph', 'langchain', 'langchain-core',
                   'langgraph-checkpoint', 'langgraph-checkpoint-postgres', 'langchain-openai', 'pydantic',
                   'openai', 'httpx', 'psycopg')}}
    return {'version': RECOVERY_CONTRACT_VERSION, 'state_schema_version': STATE_SCHEMA_VERSION,
            'sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
            'legacy_policy': 'reject', 'state_migration': 'none'}


@lru_cache(maxsize=1)
def application_revision() -> dict:
    configured = os.environ.get("APP_REVISION", "").strip()
    if configured:
        return {"revision": configured, "revision_source": "environment", "dirty": False}
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
        if revision:
            return {"revision": revision, "revision_source": "git", "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        pass
    return {"revision": "unknown", "revision_source": "unknown", "dirty": False}


def execution_manifest(*, providers=None) -> dict:
    from execution.flow_config import tool_limits
    from agent_service.tooling import load_providers
    timeouts = configured_timeouts()
    model_url = os.environ.get("AGENT_MODEL_URL", "")
    providers = load_providers() if providers is None else providers
    return {
        "application": application_revision(),
        "agent": {
            "graph_version": GRAPH_VERSION,
            "recovery_protocol": 2,
            "prompt_set_version": PROMPT_SET_VERSION,
            "tool_limits": tool_limits(),
            "acceptance_policy": {"version":1,"failure":"partial","repair_calls":0,"final_reserved_calls":2,"side_effects":"explicit_request_grants"},
        },
        "recovery_contract": recovery_contract(providers),
        "model": {
            "protocol": "openai-compatible-chat",
            "name": os.environ.get("AGENT_MODEL", ""),
            "base_url_sha256": hashlib.sha256(model_url.encode()).hexdigest(),
            "temperature": 0,
            "max_retries": 0,
        },
        "tools": {"providers": [
            {"factory": spec, "name": provider.name, "version": provider.version, "contract_version": provider.contract_version,
             "tools": [{"name": item.tool.name, "version": item.version, "kind": item.kind,
                        "produces_evidence": item.produces_evidence, "intent_spec": {"operation":item.intent_spec.operation,"effect_fields":list(item.intent_spec.effect_fields)} if item.intent_spec else None} for item in provider.tools]}
            for spec, provider in providers
        ]},
        "limits": {
            "model_calls": 16,
            "tool_calls": 10,
            "execution_timeout_seconds": int(os.environ.get("AGENT_EXECUTION_TIMEOUT_SECONDS", "300")),
            "model_timeout_seconds": timeouts.model_seconds,
            "rag_timeout_seconds": timeouts.rag_seconds,
        },
    }


def schema_version() -> int:
    return MANIFEST_SCHEMA_VERSION
