"""Service-neutral tool loading, execution context, budgeting, and trace hooks."""

import hashlib
import importlib
import json
import os
from dataclasses import dataclass, field
from uuid import UUID

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from agent_service.runtime import reserve_tool, settle_tool


DEFAULT_PROVIDERS = "agent_service.tools.rag:tools"


@dataclass
class ToolExecutionContext:
    run_id: UUID
    lease_token: UUID
    team_id: UUID | None = None
    key_id: str | None = None
    step_id: UUID | None = None
    purpose: str = "react"
    evidence: dict[str, dict] = field(default_factory=dict)
    notices: list[dict] = field(default_factory=list)
    tool_call_ids: dict[str, UUID] = field(default_factory=dict)
    tool_metadata: dict[str, dict] = field(default_factory=dict)
    triggering_model_call_id: UUID | None = None

    def business_call_id(self, tool_call_id: str | None) -> UUID:
        if not tool_call_id or tool_call_id not in self.tool_call_ids:
            raise RuntimeError("Tool call is missing its persistent trace ID")
        return self.tool_call_ids[tool_call_id]

    def add_tool_metadata(self, tool_call_id: str | None, **values) -> None:
        if not tool_call_id:
            raise RuntimeError("Tool call ID is required")
        self.tool_metadata.setdefault(tool_call_id, {}).update(values)


class FatalToolError(RuntimeError):
    def __init__(self, run_code: str, trace_code: str | None = None):
        self.run_code = run_code
        self.trace_code = trace_code or run_code
        super().__init__(run_code)


class RecoverableToolError(RuntimeError):
    def __init__(self, code: str, message: str = "Tool execution failed"):
        self.code = code
        self.message = message
        super().__init__(code)


def load_tools(specification: str | None = None) -> list[BaseTool]:
    configured = specification or os.environ.get("AGENT_TOOL_PROVIDERS", DEFAULT_PROVIDERS)
    loaded: list[BaseTool] = []
    for raw in configured.split(","):
        spec = raw.strip()
        if not spec or ":" not in spec:
            raise RuntimeError("Each AGENT_TOOL_PROVIDERS entry must be module:factory")
        module_name, factory_name = spec.rsplit(":", 1)
        factory = getattr(importlib.import_module(module_name), factory_name)
        provided = list(factory())
        if not all(isinstance(item, BaseTool) for item in provided):
            raise RuntimeError(f"Tool provider {spec} returned a non-tool value")
        loaded.extend(provided)
    names = [item.name for item in loaded]
    if len(names) != len(set(names)):
        raise RuntimeError("Configured Agent tools must have unique names")
    return loaded


def argument_summary(arguments: dict) -> dict:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return {"argument_names": sorted(arguments), "arguments_sha256": hashlib.sha256(encoded).hexdigest()}


@wrap_tool_call
def budgeted_tool(request, handler):
    context: ToolExecutionContext = request.runtime.context
    external_id = request.tool_call["id"]
    call_id = reserve_tool(
        context.run_id,
        context.lease_token,
        request.tool_call["name"],
        argument_summary(request.tool_call.get("args", {})),
        context.step_id,
        context.triggering_model_call_id,
    )
    context.tool_call_ids[external_id] = call_id
    try:
        result = handler(request)
        metadata = context.tool_metadata.pop(external_id, {})
        status = metadata.pop("status", "succeeded")
        metadata.setdefault("result_summary", {"result_type": type(result).__name__})
        if not settle_tool(context.run_id, context.lease_token, call_id, status, **metadata):
            raise PermissionError("execution lease was revoked")
        return result
    except FatalToolError as exc:
        settle_tool(context.run_id, context.lease_token, call_id, "failed", error_code=exc.trace_code)
        raise
    except RecoverableToolError as exc:
        settle_tool(context.run_id, context.lease_token, call_id, "failed", error_code=exc.code)
        return ToolMessage(
            content=json.dumps({"status": "error", "code": exc.code, "message": exc.message}),
            tool_call_id=external_id,
            name=request.tool_call["name"],
        )
    except PermissionError:
        raise
    except Exception as exc:
        settle_tool(context.run_id, context.lease_token, call_id, "failed", error_code="TOOL_CALL_FAILED", result_summary={"exception_type": type(exc).__name__})
        raise
    finally:
        context.tool_call_ids.pop(external_id, None)
        context.tool_metadata.pop(external_id, None)
