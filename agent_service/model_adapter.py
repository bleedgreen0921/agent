"""Native async model transports; custom providers require explicit registration."""
import inspect
import json
import os
import openai
from langchain_openai import ChatOpenAI
from execution.calls import ProviderReply
from execution.persistence import resources


class SyncModelDisabled:
    def __getattr__(self, _name):
        raise RuntimeError('Synchronous model operations are disabled')


_structured_adapters = {}


def register_async_model(model_type, structured_adapter):
    """Register a provider-owned native async implementation, without Runnable fallback."""
    if not isinstance(model_type, type) or not inspect.iscoroutinefunction(structured_adapter):
        raise TypeError('Model adapters require a type and a native async callable')
    _structured_adapters[model_type] = structured_adapter


def model():
    runtime = resources()
    settings = getattr(runtime, 'agent_settings', None)
    url = (settings['url'] if settings else os.environ.get('AGENT_MODEL_URL', '')).rstrip('/')
    name = settings['model']['name'] if settings else os.environ.get('AGENT_MODEL', '')
    if not url or not name:
        raise RuntimeError('AGENT_MODEL_URL and AGENT_MODEL are required')
    url = url if url.endswith('/v1') else url + '/v1'
    key_value = settings['key'] if settings else os.environ.get('AGENT_MODEL_KEY', 'local')
    cache = getattr(runtime, 'model_clients', None)
    if cache is None:
        cache = runtime.model_clients = {}
    key = (url, name, key_value)
    if key not in cache:
        root = openai.AsyncOpenAI(base_url=url, api_key=key_value, max_retries=0,
                                 http_client=runtime.http.sdk_client('agent-model'))
        cache[key] = ChatOpenAI(model=name, base_url=url, api_key=key_value,
            client=SyncModelDisabled(), root_client=SyncModelDisabled(),
            async_client=root.chat.completions, root_async_client=root,
            max_retries=0, temperature=0)
    return cache[key]


async def structured_reply(llm, schema, messages):
    if isinstance(llm, ChatOpenAI):
        response_format = {'type': 'json_schema', 'json_schema': {
            'name': schema.__name__, 'schema': schema.model_json_schema(), 'strict': False}}
        reply = await llm.bind(response_format=response_format).ainvoke(messages)
        usage = reply.usage_metadata or {}
        if not isinstance(reply.content, str):
            raise ValueError('Expected structured text')
        try: value = schema.model_validate(json.loads(reply.content))
        except ValueError as exc:
            exc.provider_usage={'input_tokens':usage.get('input_tokens'),'output_tokens':usage.get('output_tokens')}
            raise
        return ProviderReply(value, {'input_tokens': usage.get('input_tokens'),
                                    'output_tokens': usage.get('output_tokens')})
    adapter = next((_structured_adapters[cls] for cls in type(llm).__mro__
                    if cls in _structured_adapters), None)
    if adapter is None:
        raise TypeError('Custom model requires an explicitly registered native async adapter')
    reply = await adapter(llm, schema, messages)
    if not isinstance(reply, ProviderReply):
        raise TypeError('Model adapter must return ProviderReply')
    return ProviderReply(schema.model_validate(reply.value), reply.metadata)


async def structured(llm, schema, messages):
    if isinstance(llm,ChatOpenAI):return (await structured_reply(llm,schema,messages)).value
    from execution import tokens,archive
    from uuid import uuid4
    from langchain_core.messages.utils import convert_to_messages,convert_to_openai_messages
    from agent_service.model_calls import configured_model_name
    prepared=await tokens.envelope('agent-model',{'model':configured_model_name(),'messages':convert_to_openai_messages(convert_to_messages(messages)),
        'response_format':{'type':'json_schema','json_schema':{'name':schema.__name__,'schema':schema.model_json_schema(),'strict':False}}})
    identity=uuid4();handle=await tokens.reserve(identity,prepared)
    saved=await archive.prepare('agent-model',prepared.body,identity)
    await tokens.sent(handle)
    try:
        reply=await structured_reply(llm,schema,messages)
        await archive.finish(saved,{'value':reply.value.model_dump(),'metadata':reply.metadata})
        inputs=reply.metadata.get('input_tokens');outputs=reply.metadata.get('output_tokens')
        await tokens.settle(handle,tokens.UsageRecord(inputs,outputs,'provider' if inputs is not None and outputs is not None else 'unknown'))
        return reply.value
    except BaseException as exc:
        from execution.http_accounting import failed
        await failed((None,handle,saved,'agent-model'),exc,dispatched=True)
        raise


def validate_chat_model(llm):
    if isinstance(llm, ChatOpenAI):
        return
    from langchain_core.language_models.chat_models import BaseChatModel
    registered = any(cls in _structured_adapters for cls in type(llm).__mro__)
    native = inspect.iscoroutinefunction(getattr(type(llm), '_agenerate', None)) and type(llm)._agenerate is not BaseChatModel._agenerate
    if not registered or not native:
        raise TypeError('Chat provider requires registered native async generation')
