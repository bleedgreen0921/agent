"""Build the immutable execution manifest captured when a Run is claimed."""

import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from agent_service.versions import GRAPH_VERSION, MANIFEST_SCHEMA_VERSION, PROMPT_SET_VERSION
from agent_service.timeouts import configured_timeouts


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def execution_manifest() -> dict:
    from agent_service.tooling import load_providers
    timeouts = configured_timeouts()
    model_url = os.environ.get("AGENT_MODEL_URL", "")
    providers = load_providers()
    return {
        "application": application_revision(),
        "agent": {
            "graph_version": GRAPH_VERSION,
            "prompt_set_version": PROMPT_SET_VERSION,
        },
        "model": {
            "protocol": "openai-compatible-chat",
            "name": os.environ.get("AGENT_MODEL", ""),
            "base_url_sha256": hashlib.sha256(model_url.encode()).hexdigest(),
            "temperature": 0,
            "max_retries": 0,
        },
        "tools": {"providers": [
            {"factory": spec, "name": provider.name, "version": provider.version,
             "tools": [{"name": item.tool.name, "version": item.version, "kind": item.kind,
                        "produces_evidence": item.produces_evidence} for item in provider.tools]}
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
