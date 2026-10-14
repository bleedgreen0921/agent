"""Service-neutral tool loading, execution context, budgeting, and trace hooks."""
import hashlib
import importlib
import json
import os
from dataclasses import dataclass, field, replace
from contextvars import ContextVar
from asyncio import Lock
import asyncio
from agent_service.storage.receipts import MARKER, marker
from execution.persistence import PersistenceOutcomeUnknown
from execution.scope import operation_deadline, authorize
from execution.persistence import call_scope
from uuid import UUID, uuid4
from copy import deepcopy
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
import inspect
from execution.call_store import AgentCallStore
from execution.persistence import resources

async def reserve_tool(*args, **kwargs):
    return await AgentCallStore(resources()).reserve_tool(*args, **kwargs)

async def settle_tool(*args, **kwargs):
    return await AgentCallStore(resources()).settle_tool(*args, **kwargs)
from agent_service.versions import DEFAULT_TOOL_PROVIDERS
from contracts.v1 import Evidence
from execution import store
from execution.contracts import ExecutionContext as ToolExecutionContext, ToolOutcome, TaskRef, TaskKind, CONTRACT_VERSION, freeze, thaw, bounded_timeout
DEFAULT_PROVIDERS = DEFAULT_TOOL_PROVIDERS
stable_inputs = ContextVar('run_stable_inputs', default=freeze({}))

def frozen_tool_policy():
    from execution.flow_config import tool_limits
    from execution.persistence import _resources
    runtime = _resources.get()
    settings = getattr(runtime, 'agent_settings', None)
    return dict(settings['manifest']['agent']['tool_limits']) if settings else tool_limits()


@dataclass
class RunExecutionState:
    run_id: UUID
    lease_token: UUID
    step_id: UUID | None = None
    purpose: str = 'react'
    evidence: dict[str, dict] = field(default_factory=dict)
    notices: list[dict] = field(default_factory=list)
    triggering_model_call_id: UUID | None = None
    rag_timeout_seconds: float | None = None
    inputs: object = field(default_factory=lambda: stable_inputs.get())
    contributions: dict = field(default_factory=dict, repr=False)
    merge_base: object = field(default=None, repr=False)
    tool_policy: dict = field(default_factory=lambda: frozen_tool_policy(), repr=False)
    merge_lock: object = field(default_factory=Lock, repr=False)
    original_task: str = ''
    goal_contract: dict = field(default_factory=dict)
    authorizations: list[dict] = field(default_factory=list)
    completion_condition: str | None = None
    prior_summaries: list[dict] = field(default_factory=list)
    body_cache: dict = field(default_factory=dict,repr=False)

def validate_outcome(outcome, evidence):
    items = []
    seen = dict(evidence)
    for raw in outcome.evidence:
        item = Evidence.model_validate(thaw(raw)).model_dump(exclude_none=True)
        prior = seen.get(item['evidence_id'])
        if prior and {k: v for k, v in prior.items() if k != 'rank'} != {k: v for k, v in item.items() if k != 'rank'}:
            raise ValueError('Conflicting content for evidence ID')
        seen.setdefault(item['evidence_id'], item)
        items.append(item)
    return items

def merge_collections(evidence, notices, outcome):
    items = validate_outcome(outcome, evidence)
    for item in items:
        evidence.setdefault(item['evidence_id'], item)
    codes = {notice['code'] for notice in notices}
    for notice in outcome.notices:
        if notice['code'] not in codes:
            notices.append(thaw(notice))
            codes.add(notice['code'])

def merge_outcome(state, outcome):
    merge_collections(state.evidence, state.notices, outcome)

class FatalToolError(RuntimeError):

    def __init__(self, run_code: str, trace_code: str | None=None):
        self.run_code = run_code
        self.trace_code = trace_code or run_code
        super().__init__(run_code)

class RecoverableToolError(RuntimeError):

    def __init__(self, code: str, message: str='Tool execution failed'):
        self.code = code
        self.message = message
        super().__init__(code)

class ToolCallFailed(RuntimeError):
    pass

@dataclass(frozen=True)
class ToolIntentSpec:
    operation: str
    effect_fields: tuple[str, ...]

@dataclass(frozen=True)
class ToolDeclaration:
    tool: BaseTool
    version: str
    kind: str
    produces_evidence: bool
    intent_spec: ToolIntentSpec | None = None

@dataclass(frozen=True)
class ToolProvider:
    name: str
    version: str
    tools: tuple[ToolDeclaration, ...]
    contract_version: int = 0

def load_providers(specification: str | None=None) -> list[tuple[str, ToolProvider]]:
    if specification is None:
        from execution.persistence import _resources
        runtime = _resources.get()
        settings = getattr(runtime, 'agent_settings', None)
        if settings is not None:
            return settings['providers']
        if runtime is not None and getattr(runtime, 'tool_providers', None) is not None:
            return runtime.tool_providers
    configured = specification or os.environ.get('AGENT_TOOL_PROVIDERS', DEFAULT_PROVIDERS)
    loaded: list[tuple[str, ToolProvider]] = []
    names: set[str] = set()
    for raw in configured.split(','):
        spec = raw.strip()
        if not spec or ':' not in spec:
            raise RuntimeError('Each AGENT_TOOL_PROVIDERS entry must be module:factory')
        module_name, factory_name = spec.rsplit(':', 1)
        provided = getattr(importlib.import_module(module_name), factory_name)()
        if not isinstance(provided, ToolProvider) or not _nonempty(provided.name) or (not _nonempty(provided.version)):
            raise RuntimeError(f'Tool provider {spec} must declare a name and version')
        if provided.contract_version != CONTRACT_VERSION:
            raise RuntimeError(f'Tool provider {spec} must implement execution contract v{CONTRACT_VERSION}')
        if not isinstance(provided.tools, (tuple, list)):
            raise RuntimeError(f'Tool provider {spec} must declare its tools')
        for declaration in provided.tools:
            if not isinstance(declaration, ToolDeclaration) or not isinstance(declaration.tool, BaseTool) or (not _nonempty(declaration.version)) or (declaration.kind not in {'read_only', 'side_effect'}) or (type(declaration.produces_evidence) is not bool):
                raise RuntimeError(f'Tool provider {spec} has an invalid tool declaration')
            native_async = callable(getattr(declaration.tool,'coroutine',None)) if isinstance(declaration.tool,StructuredTool) else inspect.iscoroutinefunction(type(declaration.tool)._arun) and type(declaration.tool)._arun is not BaseTool._arun
            if not native_async:
                raise RuntimeError('Tools require an explicit native async or managed executor adapter')
            if declaration.kind == 'side_effect':
                intent_spec = declaration.intent_spec
                if not isinstance(intent_spec, ToolIntentSpec) or not _nonempty(intent_spec.operation) or not intent_spec.effect_fields or len(set(intent_spec.effect_fields)) != len(intent_spec.effect_fields):
                    raise RuntimeError('Side-effect tools require an explicit operation and effect fields')
                schema = declaration.tool.tool_call_schema
                schema = schema if isinstance(schema, dict) else schema.model_json_schema()
                if not set(intent_spec.effect_fields) <= set(schema.get('properties', {})):
                    raise RuntimeError('Effect fields must be model-visible tool arguments')
            annotation = str(getattr(declaration.tool, 'args_schema', ''))
            fn = getattr(declaration.tool, 'coroutine', None) or type(declaration.tool)._arun
            import typing
            hints = typing.get_type_hints(fn, include_extras=True)
            if any('InjectedStore' in str(v) or 'InjectedState' in str(v) for v in hints.values()):
                raise RuntimeError('Tools receive only isolated state and ToolOutcome; injected graph storage is forbidden')
            if declaration.tool.response_format != 'content_and_artifact':
                raise RuntimeError('Tools must return content and a ToolOutcome artifact')
            name = declaration.tool.name
            if not _nonempty(name) or name in names:
                raise RuntimeError('Configured Agent tools must have unique nonempty names')
            names.add(name)
        loaded.append((spec, provided))
    return loaded

def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())

def load_tools(specification: str | None=None) -> list[BaseTool]:
    return [declaration.tool for _, provider in load_providers(specification) for declaration in provider.tools]

def argument_summary(arguments: dict) -> dict:
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()
    return {'argument_names': sorted(arguments), 'arguments_sha256': hashlib.sha256(encoded).hexdigest()}

def tool_failure(exc):
    from execution.tokens import TokenBudgetExceeded
    if isinstance(exc,TokenBudgetExceeded):return 'QUOTA_EXCEEDED',{},exc
    from execution.infra.types import CpuPoolReset
    if isinstance(exc, FatalToolError):
        return exc.trace_code, {}, exc
    if isinstance(exc, CpuPoolReset):
        return 'CPU_POOL_RESET', {}, exc
    return 'TOOL_CALL_FAILED', {'result_summary': {'exception_type': type(exc).__name__}}, ToolCallFailed()


def _tool_context(task, token, deadline_at, step_id, call_id, seconds, inputs):
    return ToolExecutionContext(task, token, deadline_at, step_id=step_id, call_id=call_id,
                                rag_timeout_seconds=seconds, inputs=inputs)


@wrap_tool_call
async def budgeted_tool(request, handler):
    from execution.tool_capacity import tool_slot
    from execution.scope import track_call_task
    from execution.infra.types import WorkOwner
    track_call_task()
    state = request.runtime.context
    from agent_service.quality import hydrate
    from agent_service.intent import batch_identity, declarations, matching_grant
    from agent_service.acceptance import digest
    await hydrate(request.state,state)
    message=next((m for m in reversed(request.state.get('messages',[])) if any(c.get('id')==request.tool_call['id'] for c in getattr(m,'tool_calls',[]))),None)
    decision=request.state.get('tool_intents',{}).get(request.tool_call['id'])
    if not decision or not decision['allowed'] or message is None or decision['tool_name']!=request.tool_call['name'] or decision['arguments_sha256']!=digest(request.tool_call['args']) or decision['batch_hash']!=batch_identity(state.original_task,state.goal_contract,message,message.tool_calls):
        raise ToolCallFailed('Tool has no matching independent intent decision')
    declaration=declarations()[request.tool_call['name']]
    grant=matching_grant(declaration,request.tool_call['args'],state.authorizations)
    if declaration.kind=='side_effect' and (grant is None or grant!=decision['authorization_id']):
        raise ToolCallFailed('Tool explicit authorization changed or exhausted')
    from agent_service.storage.acceptance import confirm_intents
    await confirm_intents(state.run_id,state.lease_token,state.original_task,state.goal_contract,message,
                          request.state['tool_intents'],request.state.get('tool_batch_evaluation'),dispatch=True)
    call_id = uuid4()
    owner = WorkOwner(TaskRef(state.run_id, TaskKind.AGENT), state.lease_token, call_id)
    limit = await operation_deadline(state.rag_timeout_seconds or 120)
    from execution.progress import activity
    async with activity('tool:' + request.tool_call['name'], limit) as phase_id:
        async with tool_slot(owner, limit, limits=state.tool_policy, phase_id=phase_id):
            return await _execute_budgeted_tool(request, handler, call_id)


def isolated_request(request, context):
    private_state = deepcopy(request.state)
    runtime = replace(request.runtime, context=context, state=private_state, config={},
                      store=None, stream_writer=lambda value: None)
    return request.override(runtime=runtime, state=private_state, tool_call=deepcopy(request.tool_call))


async def _execute_budgeted_tool(request, handler, call_id):
    from execution.calls import CallContext, CallRunner, ProviderReply
    from execution.persistence import resources
    from execution.scope import local_authority, track_call_task
    track_call_task()
    state: RunExecutionState = request.runtime.context
    external_id = request.tool_call['id']
    task = TaskRef(state.run_id, TaskKind.AGENT)
    active = await store.check(task, state.lease_token)
    seconds = bounded_timeout(state.rag_timeout_seconds or 120, active['execution_deadline_at'], active['observed_at'])
    triggering = None
    origin = (len(request.state.get('messages', [])), 0, external_id)
    for position, message in reversed(list(enumerate(request.state.get('messages', [])))):
        if any(item.get('id') == external_id for item in getattr(message, 'tool_calls', [])):
            origin = (position, next(i for i, item in enumerate(message.tool_calls) if item['id'] == external_id), external_id)
            traced = message.additional_kwargs.get(MARKER)
            if traced and traced['kind'] == 'model':
                triggering = UUID(traced['id'])
            break
    call = CallContext(task, state.lease_token, await operation_deadline(seconds), state.step_id, request.tool_call['name'], id=call_id)
    async def reserve(call_id):
        from agent_service.intent import declarations
        declaration=declarations()[request.tool_call['name']]
        arguments=argument_summary(request.tool_call.get('args', {}))
        if declaration.kind=='side_effect':
            decision=request.state['tool_intents'][external_id]
            arguments.update(authorization_id=decision['authorization_id'],effect_arguments={k:request.tool_call['args'][k] for k in declaration.intent_spec.effect_fields})
        return await reserve_tool(state.run_id, state.lease_token, request.tool_call['name'],
            arguments, state.step_id, triggering, call_id=call_id)
    async def settle(call_id, status, **metadata):
        return await settle_tool(state.run_id, state.lease_token, call_id, status, **metadata)
    async def invoke(call):
        from execution import archive
        saved=await archive.prepare('tool:'+request.tool_call['name'],{'arguments':request.tool_call.get('args',{}),'tool_name':request.tool_call['name']},call.id)
        # Frozen snapshots may be large. Copying them is tracked blocking work.
        context = await resources().io.run(_tool_context, task, state.lease_token,
            active['execution_deadline_at'], state.step_id, call.id, seconds, state.inputs,
            owner=call.owner, deadline=call.deadline)
        private = await resources().io.run(isolated_request, request, context, owner=call.owner, deadline=call.deadline)
        try:
            result = await handler(private)
        except RecoverableToolError as exc:
            content = json.dumps({'status': 'error', 'code': exc.code, 'message': exc.message})
            outcome = ToolOutcome(content, metadata={'status': 'failed', 'error_code': exc.code})
            result = ToolMessage(content=content, artifact=outcome.to_artifact(), tool_call_id=external_id,
                                 name=request.tool_call['name'], status='error')
        if not isinstance(result, ToolMessage):
            raise ValueError('Tools must return ToolMessage content and artifact')
        if result.status == 'error' and result.artifact is None:
            outcome = ToolOutcome(str(result.content), metadata={'status': 'failed', 'error_code': 'TOOL_CALL_REJECTED'})
        else:
            outcome = ToolOutcome.from_artifact(result.artifact)
            if str(result.content) != outcome.observations:
                raise ValueError('Tool content does not match its validated artifact')
        validate_outcome(outcome, {})
        result.artifact = outcome.to_artifact()
        await archive.finish(saved,outcome.to_artifact())
        metadata = thaw(outcome.metadata)
        status = metadata.pop('status', 'succeeded')
        metadata.setdefault('result_summary', {'result_type': type(result).__name__})
        metadata.setdefault('evidence_ids', [item['evidence_id'] for item in outcome.evidence])
        return ProviderReply((result, outcome), metadata, status)
    reply = await CallRunner().run(call, reserve=reserve, invoke=invoke, settle=settle, failure=tool_failure)
    result, outcome = reply.value
    observed_start = asyncio.get_running_loop().time()
    observed = await store.check(task, state.lease_token)
    merge_until = observed_start + min((observed['execution_deadline_at'] - observed['observed_at']).total_seconds(),
        (observed.get('leased_until', observed['execution_deadline_at']) - observed['observed_at']).total_seconds())
    try:
        async with state.merge_lock:
            local_authority(task, state.lease_token)
            from agent_service.storage.bodies import resolve_one
            for item in outcome.evidence:
                key=item['evidence_id']
                if key in state.evidence:
                    state.evidence[key]=await resolve_one(state,state.evidence[key])
                    if state.merge_base is not None:
                        state.merge_base[0][key]=state.evidence[key]
            if state.merge_base is None:
                state.merge_base = (dict(state.evidence), list(state.notices))
            contributions = dict(state.contributions)
            prior = contributions.get(str(call.id))
            if prior is not None and prior != (origin, outcome):
                raise ValueError('Conflicting result for call')
            contributions[str(call.id)] = (origin, outcome)
            evidence, notices = dict(state.merge_base[0]), list(state.merge_base[1])
            for _, item in sorted(contributions.values(), key=lambda item: item[0]):
                merge_collections(evidence, notices, item)
            local_authority(task, state.lease_token)
            if asyncio.get_running_loop().time() >= merge_until:
                from execution.errors import ExecutionRevoked
                raise ExecutionRevoked('Execution authority expired during merge')
            state.evidence, state.notices = evidence, notices
            state.contributions = contributions
    except ValueError as exc:
        # The provider succeeded; a Run-level conflict must not re-settle it.
        raise ToolCallFailed('Conflicting tool evidence') from exc
    result.additional_kwargs[MARKER] = marker('tool', call.id)
    from agent_service.storage.bodies import put_json
    body=await put_json(state.run_id,state.lease_token,outcome.to_artifact())
    result.artifact={'_tool_body':body,'notices':[thaw(n) for n in outcome.notices]}
    result.content=json.dumps({'status':reply.status,'evidence_catalog':[
        {'evidence_id':item['evidence_id'],'title':item.get('title',''),'source_kind':item.get('source_locator',{}).get('kind'),
         'bytes':len(json.dumps(thaw(item),ensure_ascii=False,separators=(',',':')).encode())}
        for item in outcome.evidence], 'observation_preview':outcome.observations[:1024],
        'body_available':True},ensure_ascii=False)
    return result


def managed_io_tool(function, *, name, description, args_schema, timeout_seconds=120):
    """Explicit blocking adapter. Functions receive frozen data, never async clients."""
    from execution.persistence import resources
    from execution.scope import work_owner
    async def invoke(**arguments):
        runtime = arguments.pop('runtime')
        limit = await operation_deadline(timeout_seconds)
        await authorize()
        return await resources().io.run(function, runtime.context, arguments,
            owner=work_owner(runtime.context.call_id), deadline=limit, before_submit=authorize)
    # ToolRuntime remains an injected argument rather than model-controlled data.
    from langchain.tools import ToolRuntime
    invoke.__annotations__ = {'runtime': ToolRuntime[ToolExecutionContext]}
    invoke.__signature__ = inspect.Signature([
        *[inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY,
             annotation=field.annotation, default=inspect.Parameter.empty if field.is_required() else field.default)
          for key, field in args_schema.model_fields.items()],
        inspect.Parameter('runtime', inspect.Parameter.KEYWORD_ONLY, annotation=ToolRuntime[ToolExecutionContext])])
    return StructuredTool.from_function(coroutine=invoke, name=name, description=description,
        args_schema=args_schema, response_format='content_and_artifact')
