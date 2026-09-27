"""Validated call timeouts and immutable per-Run timeout settings."""

import math
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CallTimeouts:
    model_seconds: float
    rag_seconds: float


def positive_seconds(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return seconds


def configured_timeouts() -> CallTimeouts:
    return CallTimeouts(
        positive_seconds(os.environ.get("AGENT_MODEL_TIMEOUT_SECONDS", "120"), "AGENT_MODEL_TIMEOUT_SECONDS"),
        positive_seconds(os.environ.get("AGENT_RAG_TIMEOUT_SECONDS", "120"), "AGENT_RAG_TIMEOUT_SECONDS"),
    )


def timeouts_for_manifest(manifest: dict | None) -> CallTimeouts:
    current = configured_timeouts()
    limits = (manifest or {}).get("limits", {})
    return CallTimeouts(
        positive_seconds(limits.get("model_timeout_seconds", current.model_seconds), "manifest model_timeout_seconds"),
        positive_seconds(limits.get("rag_timeout_seconds", current.rag_seconds), "manifest rag_timeout_seconds"),
    )
