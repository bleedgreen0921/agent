"""Request-local token estimates and durable, conservative task accounting.

Estimated counts are admission estimates, never a promise about a provider's
tokenizer. An unconfirmed dispatch retains its entire reservation.
"""
import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb
from execution.persistence import transaction, resources, deadline, PersistenceOutcomeUnknown
from execution.retry import retry_transaction
from execution.scope import current_attempt, work_owner

budget_context = ContextVar('token_budget', default=None)
purpose_context = ContextVar('model_purpose', default='')


class InputTooLarge(ValueError):
    pass


class TokenBudgetExceeded(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class ModelTokenPolicy:
    context: int = 32768
    output: int = 4096
    tokenizer: str | None = None
    version: int = 1
    artifact_sha256: str | None = None

    def __post_init__(self):
        if type(self.context) is not int or type(self.output) is not int or not 0 < self.output < self.context:
            raise ValueError('Token policy requires 0 < output < context')


@dataclass(frozen=True)
class RequestEnvelope:
    service: str
    body: dict
    model: str
    input_tokens: int
    output_tokens: int
    estimation: str
    sha256: str


@dataclass(frozen=True)
class UsageRecord:
    input_tokens: int | None
    output_tokens: int | None
    source: str
    charged: int | None = None


def tokenizer_fingerprint(root):
    from pathlib import Path
    root = Path(root).resolve(strict=True)
    if not root.is_dir(): raise ValueError('Tokenizer must be a local directory')
    digest = hashlib.sha256()
    for name in ('tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.json',
                 'merges.txt','added_tokens.json','vocab.txt','tokenizer.model','spiece.model','sentencepiece.bpe.model'):
        file = root / name
        if file.exists():
            if file.is_symlink() or file.stat().st_size > 32*1024*1024:
                raise ValueError('Tokenizer artifact is invalid')
            digest.update(name.encode()+b'\0'+file.read_bytes())
    return str(root), digest.hexdigest()


def configuration():
    models = json.loads(os.environ.get('TOKEN_MODEL_POLICIES', '{}'))
    if not isinstance(models, dict): raise ValueError('TOKEN_MODEL_POLICIES must be an object')
    for value in models.values():
        ModelTokenPolicy(**value)
        if value.get('tokenizer'):
            value['tokenizer'], value['artifact_sha256'] = tokenizer_fingerprint(value['tokenizer'])
    limits = {kind: int(os.environ.get(env, str(default))) for kind, env, default in (
        ('agent_run','AGENT_TOKEN_LIMIT',262144), ('memory_job','MEMORY_TOKEN_LIMIT',65536),
        ('rag_document','RAG_DOCUMENT_TOKEN_LIMIT',1048576), ('search','RAG_SEARCH_TOKEN_LIMIT',98304))}
    if any(value <= 0 for value in limits.values()): raise ValueError('Token limits must be positive')
    return {'version':1, 'models':models, 'limits':limits}


def settings():
    runtime = resources()
    if not hasattr(runtime, 'token_settings'): runtime.token_settings = configuration()
    return runtime.token_settings


def policy(model):
    return ModelTokenPolicy(**settings()['models'].get(model, {}))


def _count(body, selected, cache, lock):
    text = canonical(body)
    if selected.tokenizer:
        from transformers import AutoTokenizer
        # Explicit local files only, never lazy downloads or remote Python code.
        key=(selected.tokenizer,selected.artifact_sha256)
        with lock:
            if key not in cache:
                if tokenizer_fingerprint(selected.tokenizer)[1] != selected.artifact_sha256:
                    raise ValueError('Tokenizer artifacts changed after policy capture')
                cache[key]=AutoTokenizer.from_pretrained(selected.tokenizer, local_files_only=True, trust_remote_code=False)
            tokenizer=cache[key]
        if 'messages' in body:
            if not tokenizer.chat_template: raise ValueError('Configured chat tokenizer has no template')
            count = len(tokenizer.apply_chat_template(body['messages'], tools=body.get('tools'), tokenize=True, add_generation_prompt=True))
            extra = {k:v for k,v in body.items() if k not in ('messages','tools')}
            return count + len(tokenizer.encode(canonical(extra), add_special_tokens=False)), 'local_tokenizer'
        return len(tokenizer.encode(text, add_special_tokens=True)), 'local_tokenizer'
    return len(text.encode('utf-8')) + 1024, 'estimated_bytes'


async def envelope(service, body):
    selected = policy(str(body.get('model','unknown')))
    value = dict(body)
    output = 0
    if service in ('agent-model','rewrite'):
        requested = [value[k] for k in ('max_tokens','max_completion_tokens') if k in value] or [selected.output]
        if any(type(v) is not int or v <= 0 for v in requested): raise ValueError('Invalid model output limit')
        output = min(*requested, selected.output)
        key = 'max_completion_tokens' if 'max_completion_tokens' in value else 'max_tokens'
        if key == 'max_completion_tokens': value.pop('max_tokens', None)
        value[key] = output
    runtime=resources()
    if not hasattr(runtime,'tokenizer_cache'):
        import threading
        runtime.tokenizer_cache={};runtime.tokenizer_lock=threading.Lock()
    count, method = await runtime.io.run(_count, value, selected,runtime.tokenizer_cache,runtime.tokenizer_lock,owner=work_owner(),deadline=deadline(30))
    if count + output > selected.context:
        raise InputTooLarge(f'Request input {count} + output {output} exceeds context {selected.context} ({method})')
    return RequestEnvelope(service,value,str(value.get('model','unknown')),count,output,method,
                           hashlib.sha256(canonical(value).encode()).hexdigest())


def scope():
    runtime = resources()
    namespace = 'agent' if runtime.role.startswith('agent') else 'rag'
    override = budget_context.get()
    if override: return namespace, override['id'], override.get('task_id'), override['kind'], override.get('limit')
    attempt = current_attempt()
    if attempt: return namespace,str(attempt.task.id),attempt.task.id,str(attempt.task.kind),None
    return namespace,'request:' + str(uuid4()),None,'search',None


@retry_transaction
async def reserve(identity, prepared, *, budget_scope=None, parent=None, held=None):
    namespace,budget_id,task_id,kind,limit = budget_scope or scope()
    config = settings()
    amount = held if held is not None else prepared.input_tokens + prepared.output_tokens
    async with transaction() as conn:
        attempt = current_attempt()
        await check_allocation(conn)
        if attempt:
            from execution import store
            await store.assert_active(conn,attempt.task,attempt.token)
        await conn.execute('INSERT INTO execution.token_budgets(namespace,id,task_id,kind,policy,token_limit) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                           (namespace,budget_id,task_id,kind,Jsonb(config),limit or config['limits'][kind]))
        budget = await (await conn.execute('SELECT * FROM execution.token_budgets WHERE namespace=%s AND id=%s FOR UPDATE',(namespace,budget_id))).fetchone()
        if budget['policy'] != config: raise ValueError('Frozen token policy differs from runtime')
        prior = await (await conn.execute('SELECT * FROM execution.token_calls WHERE namespace=%s AND id=%s',(namespace,identity))).fetchone()
        if prior:
            if prior['budget_id'] != budget_id or prior['request_sha256'] != prepared.sha256: raise ValueError('Token reservation identity conflict')
            raise PersistenceOutcomeUnknown('Existing token reservation cannot authorize redispatch')
        final_hold = 0
        if kind == 'agent_run':
            final_hold = 2 * policy(prepared.model).context
            if purpose_context.get() in ('final','final_acceptance_part'): final_hold //= 2
            elif purpose_context.get() == 'final_acceptance': final_hold = 0
        if budget['blocked'] or budget['charged'] + amount + final_hold > budget['token_limit']:
            raise TokenBudgetExceeded('Task token budget exhausted')
        await conn.execute('UPDATE execution.token_budgets SET charged=charged+%s WHERE namespace=%s AND id=%s',(amount,namespace,budget_id))
        await conn.execute('''INSERT INTO execution.token_calls(namespace,budget_id,id,model,service,request_sha256,
            input_estimate,output_reserved,reserved,charged,estimation,status,attempt_token,parent_call_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s)''',
            (namespace,budget_id,identity,prepared.model,prepared.service,prepared.sha256,prepared.input_tokens,
             prepared.output_tokens,amount,amount,prepared.estimation,attempt.token if attempt else None,parent))
        if attempt: await store.assert_active(conn,attempt.task,attempt.token)
        await check_allocation(conn)
    return namespace, identity


@retry_transaction
async def sent(handle):
    async with transaction() as conn:
        await check_allocation(conn)
        attempt=current_attempt()
        if attempt:
            from execution import store
            await store.assert_active(conn,attempt.task,attempt.token)
        current=await (await conn.execute('SELECT budget_id FROM execution.token_calls WHERE namespace=%s AND id=%s',handle)).fetchone()
        if not current:raise ValueError('Unknown token reservation')
        budget=await (await conn.execute('SELECT blocked FROM execution.token_budgets WHERE namespace=%s AND id=%s FOR UPDATE',(handle[0],current['budget_id']))).fetchone()
        if budget['blocked']:raise TokenBudgetExceeded('Task token accounting blocked further dispatch')
        row=await (await conn.execute("UPDATE execution.token_calls SET status='sent' WHERE namespace=%s AND id=%s AND status='reserved' RETURNING id",handle)).fetchone()
        if not row: raise PersistenceOutcomeUnknown('Dispatch token is not fresh')
        if attempt: await store.assert_active(conn,attempt.task,attempt.token)
        await check_allocation(conn)


async def check_allocation(conn):
    value=budget_context.get() or {}
    if value.get('expires_at') is not None:
        row=await (await conn.execute('SELECT clock_timestamp()>=to_timestamp(%s) AS expired',(value['expires_at'],))).fetchone()
        if row['expired']:raise TokenBudgetExceeded('Delegated allocation expired')


def usage(payload, service):
    value = payload.get('usage',{}) if isinstance(payload,dict) else {}
    inputs=value.get('prompt_tokens',value.get('input_tokens',value.get('total_tokens') if service in ('embedding','rerank') else None))
    outputs=value.get('completion_tokens',value.get('output_tokens',0 if service in ('embedding','rerank') and inputs is not None else None))
    if any(v is not None and (type(v) is not int or v < 0) for v in (inputs,outputs)):
        return UsageRecord(None,None,'unknown')
    return UsageRecord(inputs,outputs,'provider' if inputs is not None and outputs is not None else 'unknown')


@retry_transaction
async def settle(handle, record=None, *, not_sent=False):
    async with transaction() as conn:
        attempt = current_attempt()
        if attempt:
            from execution import store
            await store.assert_active(conn,attempt.task,attempt.token)
        row=await (await conn.execute('SELECT * FROM execution.token_calls WHERE namespace=%s AND id=%s',handle)).fetchone()
        if not row: raise ValueError('Unknown token reservation')
        await conn.execute('SELECT 1 FROM execution.token_budgets WHERE namespace=%s AND id=%s FOR UPDATE',(handle[0],row['budget_id']))
        row=await (await conn.execute('SELECT * FROM execution.token_calls WHERE namespace=%s AND id=%s FOR UPDATE',handle)).fetchone()
        if row['status'] in ('settled','released'):
            if record is not None and record.source=='provider' and (row['input_tokens']!=record.input_tokens or row['output_tokens']!=record.output_tokens):
                raise ValueError('Token settlement identity conflict')
            return
        if not_sent and row['status'] != 'reserved': raise ValueError('Dispatched tokens cannot be released')
        actual = record is not None and record.source in ('provider','delegated')
        amount = 0 if not_sent else (record.charged if record.charged is not None else record.input_tokens + record.output_tokens) if actual else row['reserved']
        await conn.execute('UPDATE execution.token_budgets SET charged=charged+%s WHERE namespace=%s AND id=%s',(amount-row['charged'],handle[0],row['budget_id']))
        if actual and row['service']!='rag' and (record.input_tokens>row['input_estimate'] or record.output_tokens>row['output_reserved']):
            await conn.execute('UPDATE execution.token_budgets SET blocked=true WHERE namespace=%s AND id=%s',(handle[0],row['budget_id']))
        await conn.execute('UPDATE execution.token_calls SET charged=%s,input_tokens=%s,output_tokens=%s,status=%s,finished_at=clock_timestamp() WHERE namespace=%s AND id=%s',
            (amount,record.input_tokens if actual else None,record.output_tokens if actual else None,
             'released' if not_sent else 'settled' if actual and record.source=='provider' else 'unknown',*handle))
        if attempt: await store.assert_active(conn,attempt.task,attempt.token)


async def report(budget_id):
    namespace='agent' if resources().role.startswith('agent') else 'rag'
    async with transaction(readonly=True) as conn:
        budget=await (await conn.execute('SELECT token_limit,charged,blocked FROM execution.token_budgets WHERE namespace=%s AND id=%s',(namespace,str(budget_id)))).fetchone()
        totals=await (await conn.execute('''SELECT count(*) AS calls,coalesce(sum(input_tokens),0) AS actual_input_tokens,
            coalesce(sum(output_tokens),0) AS actual_output_tokens,
            coalesce(sum(greatest(0,charged-coalesce(input_tokens,0)-coalesce(output_tokens,0))) FILTER(WHERE status NOT IN ('settled','released')),0) AS unconfirmed_tokens,
            count(*) FILTER(WHERE estimation='estimated_bytes') AS estimated_calls
            FROM execution.token_calls WHERE namespace=%s AND budget_id=%s''',(namespace,str(budget_id)))).fetchone()
    return {**(dict(budget) if budget else {'token_limit':None,'charged':0}),**{k:int(v) for k,v in totals.items()}}
