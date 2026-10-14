"""Agent attempt coordination and durable finalization, outside business graphs."""
import os
import logging
log = logging.getLogger(__name__)
from uuid import UUID
from agent_service import storage
from agent_service.storage import graph as graph_store
from agent_service.runtime import BudgetExhausted, fail_run, publish_result
from agent_service.checkpoints import protected_saver, with_agent_search_path
from agent_service.timeouts import _run_timeouts, current_timeouts, timeouts_for_manifest
from agent_service.tooling import RunExecutionState, stable_inputs, FatalToolError, ToolCallFailed
from agent_service.model_calls import ModelAuthError, ModelTimeoutError
from agent_service.schemas import InvalidPlanError
from execution.contracts import TaskRef, TaskKind
from execution.errors import CONTROL_ERRORS, ExecutionRevoked, select_failure
from execution.outcomes import AttemptOutcome, OutcomeKind
from execution.scope import attempt, stop_children


def failure_code(exc):
    from execution.tokens import InputTooLarge,TokenBudgetExceeded
    if isinstance(exc,InputTooLarge):return 'INPUT_TOKEN_LIMIT_EXCEEDED'
    if isinstance(exc,TokenBudgetExceeded):return 'QUOTA_EXCEEDED'
    from execution.infra.types import CpuPoolReset
    from agent_service.model_calls import ToolBatchTooLarge, ModelRateLimited
    from execution.tool_capacity import ToolCapacityExceeded
    if isinstance(exc, ToolBatchTooLarge): return "TOOL_BATCH_TOO_LARGE"
    if isinstance(exc, ToolCapacityExceeded): return "TOOL_CAPACITY_EXCEEDED"
    if isinstance(exc, ModelRateLimited): return "MODEL_RATE_LIMITED"
    if isinstance(exc, InvalidPlanError): return 'INVALID_PLAN'
    if isinstance(exc, ModelAuthError): return 'MODEL_AUTH_FAILED'
    if isinstance(exc, ModelTimeoutError): return 'MODEL_TIMEOUT'
    if isinstance(exc, FatalToolError): return exc.run_code
    if isinstance(exc, ToolCallFailed): return 'TOOL_CALL_FAILED'
    if isinstance(exc, CpuPoolReset): return 'CPU_POOL_RESET'
    if isinstance(exc, BudgetExhausted): return 'QUOTA_EXCEEDED'
    return 'MODEL_CALL_FAILED'


async def execute_run(run_id: UUID, token: UUID):
    from execution.inputs import prepare_inputs
    task = TaskRef(run_id, TaskKind.AGENT)
    # Nested binding preserves the supervisor's stop signal and deadline.
    with attempt(task, token):
        from agent_service.startup import validate_execution
        await validate_execution(run_id, token)
        try:
            inputs = await prepare_inputs(task, token)
        except CONTROL_ERRORS:
            raise
        except Exception as exc:
            selected = select_failure(exc)
            if isinstance(selected, CONTROL_ERRORS):
                raise selected
            if not await fail_run(run_id, token, 'TOOL_CALL_FAILED', type(exc).__name__):
                raise ExecutionRevoked('Run input finalization rejected') from exc
            return AttemptOutcome(OutcomeKind.FINISHED, 'TOOL_CALL_FAILED')
        key = stable_inputs.set(inputs)
        try:
            await execute_graph(run_id, token)
            return AttemptOutcome(OutcomeKind.FINISHED)
        except CONTROL_ERRORS:
            raise
        except Exception as exc:
            selected = select_failure(exc)
            if isinstance(selected, CONTROL_ERRORS):
                raise selected
            code = failure_code(exc)
            log.warning('Agent attempt failed (%s)', type(exc).__name__ + (': '+str(exc) if type(exc).__name__=='InputTooLarge' else ''), extra={'run_id': str(run_id),
                        'error_code': code, 'exception_type': type(exc).__name__})
            if not await fail_run(run_id, token, code, type(exc).__name__):
                raise ExecutionRevoked('Run finalization rejected') from exc
            return AttemptOutcome(OutcomeKind.FINISHED, code)
        finally:
            try:
                await stop_children()
            finally:
                stable_inputs.reset(key)


async def execute_graph(run_id, token):
    from agent_service import execution as graph
    from agent_service.memory import capture_snapshot, prompt_context
    run = await graph_store.load_run(run_id, token)
    if not run:
        raise ExecutionRevoked('Run no longer belongs to this attempt')
    key = _run_timeouts.set(timeouts_for_manifest(run['manifest']))
    try:
        snapshot = await capture_snapshot(run_id, token, run['task'])
        memory_context = prompt_context(snapshot)
        contextual_task = run['task'] + ('\n\nConversation context (lower trust):\n' + memory_context if memory_context else '')
        notices = [{'code': 'MEMORY_DEGRADED', 'message': 'Personal memory retrieval was unavailable; available conversation context was used'}] if snapshot.get('memory_degraded') else []
        dsn = with_agent_search_path(os.environ['AGENT_DATABASE_URL'], runtime=True)
        async with protected_saver(dsn, TaskRef(run_id, TaskKind.AGENT), token) as saver:
            if run['mode'] == 'react':
                context = RunExecutionState(run_id, token, notices=notices,
                                            rag_timeout_seconds=current_timeouts().rag_seconds,original_task=run['task'])
                agent = graph.react_agent(saver)
                config = {'configurable': {'thread_id': str(run_id)}, 'recursion_limit': 64}
                saved = await agent.aget_state(config)
                await graph.collect_evidence(saved.values.get('messages', []) if saved.values else [], context.evidence, context.notices,context=context)
                partial = False
                try:
                    output = await agent.ainvoke(None if saved.values else {'messages': [{'role': 'user', 'content': contextual_task}]},
                        config=config, context=context, durability='sync')
                except (BudgetExhausted,__import__('execution.tokens',fromlist=['InputTooLarge']).InputTooLarge,
                        __import__('execution.tokens',fromlist=['TokenBudgetExceeded']).TokenBudgetExceeded):
                    partial = True
                    output = dict((await agent.aget_state(config)).values or {})
                await graph.collect_evidence(output.get('messages', []), context.evidence, context.notices,context=context)
                context.goal_contract=output.get('goal_contract',{})
                partial=partial or bool(output.get('acceptance_blocked'))
                if output.get('acceptance_reason'):
                    from agent_service.acceptance import public_issue
                    context.notices.append({'code':'EXECUTION_ACCEPTANCE_PARTIAL','message':public_issue(output['acceptance_reason'])})
                summaries=[output['execution_summary']] if output.get('execution_summary') else []
            else:
                summaries, evidence, recovered, partial = await graph.run_plan(run_id, token, run['task'], saver,memory_context=memory_context)
                from agent_service.storage.acceptance import load_goal
                context = RunExecutionState(run_id, token, evidence=evidence, notices=[*notices, *recovered],
                                            rag_timeout_seconds=current_timeouts().rag_seconds,original_task=run['task'],
                                            goal_contract=await load_goal(run_id,token))
            # There is exactly one final-generation path, including partial runs.
            draft = await graph.final_generate(run['task'], summaries, context, partial, memory_context)
            if not await publish_result(run_id, token, draft, context.evidence, draft.partial,
                                        'acceptance_partial' if draft.partial else 'acceptance_passed'):
                raise ExecutionRevoked('Result publication rejected')
    finally:
        _run_timeouts.reset(key)
