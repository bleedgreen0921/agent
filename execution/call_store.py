"""Explicit resource-injected call ledger interface used by invocation adapters."""
from uuid import UUID
from execution.persistence import using, Applied, AlreadyApplied


class AgentCallStore:
    def __init__(self, runtime):
        self.runtime = runtime

    async def reserve_model(self, run_id: UUID, token: UUID, purpose: str, model: str,
                            step_id=None, final=False, input_summary=None, *, call_id: UUID):
        from agent_service.storage.runtime import _reserve
        with using(self.runtime):
            return await _reserve(run_id, token, 'model', purpose, model, step_id, final,
                                  input_summary=input_summary, call_id=call_id, outcome=True)

    async def reserve_tool(self, run_id: UUID, token: UUID, tool_name: str, arguments: dict,
                           step_id=None, triggering_model_call_id=None, *, call_id: UUID):
        from agent_service.storage.runtime import _reserve
        with using(self.runtime):
            return await _reserve(run_id, token, 'tool', tool_name, '', step_id, False,
                arguments=arguments, triggering_model_call_id=triggering_model_call_id,
                call_id=call_id, outcome=True)

    async def settle_model(self, run_id, token, call_id, status, **metadata):
        from agent_service.storage.runtime import settle_model
        with using(self.runtime):
            return await settle_model(run_id, token, call_id, status, **metadata)

    async def settle_tool(self, run_id, token, call_id, status, **metadata):
        from agent_service.storage.runtime import settle_tool
        with using(self.runtime):
            return await settle_tool(run_id, token, call_id, status, **metadata)
