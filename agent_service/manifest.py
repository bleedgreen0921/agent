"""Build the immutable execution manifest captured when a Run is claimed."""

import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from agent_service.versions import DEFAULT_TOOL_PROVIDERS, GRAPH_VERSION, MANIFEST_SCHEMA_VERSION, PROMPT_SET_VERSION


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
    model_url = os.environ.get("AGENT_MODEL_URL", "")
    providers = [item.strip() for item in os.environ.get("AGENT_TOOL_PROVIDERS", DEFAULT_TOOL_PROVIDERS).split(",") if item.strip()]
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
        "tools": {"providers": providers},
        "limits": {
            "model_calls": 16,
            "tool_calls": 10,
            "execution_timeout_seconds": int(os.environ.get("AGENT_EXECUTION_TIMEOUT_SECONDS", "300")),
        },
    }


def schema_version() -> int:
    return MANIFEST_SCHEMA_VERSION
