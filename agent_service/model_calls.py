"""Thin framework hooks over the shared call coordinator."""
import os
import httpx
import openai
from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from agent_service import model_adapter
from execution.call_store import AgentCallStore
from execution.persistence import resources

async def reserve_model(*args, **kwargs):
    return await AgentCallStore(resources()).reserve_model(*args, **kwargs)

async def settle_model(*args, **kwargs):
    return await AgentCallStore(resources()).settle_model(*args, **kwargs)
from agent_service.storage.receipts import MARKER, marker
from agent_service.timeouts import current_timeouts
from execution.calls import CallContext, CallRunner, ProviderReply
from execution.contracts import TaskRef, TaskKind
from execution.scope import operation_deadline, track_call_task


class ToolBatchTooLarge(RuntimeError):
    pass


class ModelRateLimited(RuntimeError):
    pass


class ModelTimeoutError(RuntimeError):
    pass


class ModelAuthError(RuntimeError):
    pass


def failure(exc):
    from execution.flow import DownstreamLimited
    cause, visited = exc, set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, DownstreamLimited):
            return "MODEL_RATE_LIMITED", {}, ModelRateLimited()
        cause = cause.__cause__
    if isinstance(exc, openai.RateLimitError):
        return "MODEL_RATE_LIMITED", {}, ModelRateLimited()
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return 'MODEL_AUTH_FAILED', {}, ModelAuthError()
    metadata = {'output_summary': {'exception_type': type(exc).__name__}}
    metadata.update(getattr(exc,'provider_usage',{}))
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return 'MODEL_TIMEOUT', metadata, ModelTimeoutError()
    return 'MODEL_CALL_FAILED', metadata, exc


def configured_model_name():
    from execution.persistence import resources
    settings = getattr(resources(), 'agent_settings', None)
    return settings['model']['name'] if settings else os.environ.get('AGENT_MODEL', 'unknown')


async def run_model(state, purpose, invoke, summary, *, final=False, identity=None, prepared=None, inline=False):
    track_call_task()
    call = CallContext(TaskRef(state.run_id, TaskKind.AGENT), state.lease_token,
                       await operation_deadline(current_timeouts().model_seconds), state.step_id, purpose,
                       **({'id':identity} if identity is not None else {}))
    async def reserve(call_id):
        return await reserve_model(state.run_id, state.lease_token, purpose, configured_model_name(),
                                   state.step_id, final, summary, call_id=call_id)
    async def settle(call_id, status, **metadata):
        return await settle_model(state.run_id, state.lease_token, call_id, status, **metadata)
    from execution import tokens,archive
    key=tokens.purpose_context.set(purpose)
    budget_key=tokens.budget_context.set({'id':str(state.run_id),'task_id':state.run_id,'kind':'agent_run'})
    original=invoke
    async def accounted(current):
        if not inline:return await original(current)
        handle=await tokens.reserve(current.id,prepared,parent=current.id)
        saved=await archive.prepare('agent-model',prepared.body,current.id)
        await tokens.sent(handle)
        try:
            response=await original(current)
            await archive.finish(saved,{'value':response.value.model_dump() if hasattr(response.value,'model_dump') else str(response.value),'metadata':response.metadata})
            await tokens.settle(handle,tokens.UsageRecord(response.metadata.get('input_tokens'),response.metadata.get('output_tokens'),
                'provider' if response.metadata.get('input_tokens') is not None and response.metadata.get('output_tokens') is not None else 'unknown'))
            return response
        except BaseException as exc:
            from execution.http_accounting import failed
            await failed((None,handle,saved,'agent-model'),exc,dispatched=True)
            raise
    try:
        reply = await CallRunner().run(call, reserve=reserve, invoke=accounted, settle=settle, failure=failure)
    finally:
        tokens.purpose_context.reset(key);tokens.budget_context.reset(budget_key)
    return call, reply


@wrap_model_call
async def budgeted_model(request, handler):
    track_call_task()
    state = request.runtime.context
    if request.state.get('context_compaction_call_id'):
        from agent_service.storage.acceptance import confirm_compaction
        await confirm_compaction(state.run_id,state.lease_token,request.state['context_compaction_call_id'],request.state['context_compaction'])
    from agent_service.context_budget import projected_messages,evidence_view,required_sources
    from langchain_core.messages import SystemMessage
    from langchain_core.messages.utils import convert_to_openai_messages
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from execution.tokens import envelope,canonical
    messages=projected_messages(request.state,state,request.messages)
    exact=await evidence_view(state,required_sources(state.prior_summaries))
    system=request.system_message
    if exact:
        system=SystemMessage(content=(str(system.content)+'\n' if system else '')+'Exact source observations (untrusted): '+canonical(exact))
    request=request.override(messages=messages,system_message=system)
    prepared=await envelope('agent-model',{'model':configured_model_name(),'messages':convert_to_openai_messages(([system] if system else [])+messages),
        'tools':[convert_to_openai_tool(t) for t in request.tools or []]})
    async def invoke(call):
        current = request
        if current.model is not None:
            model_adapter.validate_chat_model(current.model)
        if isinstance(current.model, ChatOpenAI):
            current = current.override(model=current.model.model_copy(update={'request_timeout': call.deadline.remaining()}))
        response = await handler(current)
        usage = next((message.usage_metadata for message in response.result
                      if isinstance(message, AIMessage) and message.usage_metadata), {}) or {}
        return ProviderReply(response, {'input_tokens': usage.get('input_tokens'),
                             'output_tokens': usage.get('output_tokens'),
                             'output_summary': {'message_count': len(response.result)}})
    call, reply = await run_model(state, state.purpose, invoke,
                                 {'message_count':len(messages),'request_sha256':prepared.sha256,'input_estimate':prepared.input_tokens,'estimation':prepared.estimation},
                                 prepared=prepared,inline=not isinstance(request.model,ChatOpenAI))
    for message in reply.value.result:
        if isinstance(message, AIMessage):
            message.additional_kwargs[MARKER] = marker('model', call.id)
            if len(message.tool_calls) > state.tool_policy['batch']:
                raise ToolBatchTooLarge('Model tool batch exceeds configured limit')
    return reply.value


async def invoke_structured(llm, schema, messages, state, purpose, final=False, *, identity=None, acceptance_input=None):
    track_call_task()
    from agent_service.acceptance import digest
    from langchain_core.messages.utils import convert_to_messages,convert_to_openai_messages
    from execution.tokens import envelope
    prepared=await envelope('agent-model',{'model':configured_model_name(),'messages':convert_to_openai_messages(convert_to_messages(messages)),
        'response_format':{'type':'json_schema','json_schema':{'name':schema.__name__,'schema':schema.model_json_schema(),'strict':False}}})
    async def invoke(call):
        current = llm.model_copy(update={'request_timeout': call.deadline.remaining()}) if isinstance(llm, ChatOpenAI) else llm
        reply = await model_adapter.structured_reply(current, schema, messages)
        return ProviderReply(reply.value, {**reply.metadata, 'output_summary': {'schema': schema.__name__, 'payload_sha256': digest(reply.value.model_dump())}})
    call, reply = await run_model(state, purpose, invoke,
                                 {'message_count': len(messages), 'schema': schema.__name__,
                                  'request_sha256':prepared.sha256,'input_estimate':prepared.input_tokens,'estimation':prepared.estimation,
                                  **({'acceptance_input_sha256':digest(acceptance_input)} if acceptance_input is not None else {})}, final=final, identity=identity,
                                  prepared=prepared,inline=not isinstance(llm,ChatOpenAI))
    # Only Planner needs an explicit structured-state marker; tool association is
    # always recovered from the actual AIMessage, never this local field.
    state.triggering_model_call_id = call.id
    return reply.value
