"""Validated operational limits. No credentials or raw URLs are persisted."""
import hashlib
import json
import math
import os
from urllib.parse import urlsplit


def number(name, default, *, integer=False):
    raw = os.environ.get(name, str(default))
    value = int(raw) if integer else float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be positive and finite')
    return value


def tool_limits():
    return {key: number(env, default, integer=True) for key, env, default in (
        ('run', 'AGENT_TOOL_CONCURRENCY', 2), ('worker', 'AGENT_TOOL_WORKER_CONCURRENCY', 4),
        ('pending', 'AGENT_TOOL_PENDING', 16), ('batch', 'AGENT_TOOL_BATCH_LIMIT', 10))}


def admission_policy(kind):
    prefix, rate, burst = {'agent_run': ('AGENT', 10 / 60, 3),
                           'rag_document': ('RAG_DOCUMENT', 6 / 60, 2),
                           'search': ('RAG_SEARCH', 2, 4)}[kind]
    return {'rate': number(prefix + '_SUBMIT_RATE', rate),
            'burst': number(prefix + '_SUBMIT_BURST', burst, integer=True),
            'queued': number(prefix + '_QUEUE_CAPACITY', 8, integer=True),
            'unfinished': number(prefix + '_UNFINISHED_CAPACITY', 8 if kind == 'search' else 10, integer=True)}


def downstream_policy(service, url):
    parsed = urlsplit(str(url))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Invalid downstream origin')
    origin = f'{parsed.scheme}://{parsed.hostname.lower()}:{parsed.port or (443 if parsed.scheme == "https" else 80)}'
    groups = json.loads(os.environ.get('DOWNSTREAM_GROUPS', '{}'))
    name = groups.get(service, origin)
    if not isinstance(name, str) or not name:
        raise ValueError('Invalid downstream group')
    overrides = json.loads(os.environ.get('DOWNSTREAM_LIMITS', '{}')).get(name, {})
    rate = float(overrides.get('rate', number('DOWNSTREAM_REQUEST_RATE', 1)))
    burst = overrides.get('burst', number('DOWNSTREAM_REQUEST_BURST', 2, integer=True))
    if not math.isfinite(rate) or rate <= 0 or type(burst) is not int or burst <= 0:
        raise ValueError('Invalid downstream quota')
    return hashlib.sha256(name.encode()).hexdigest(), {'rate': rate, 'burst': burst}


def policy_hash(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(role=None):
    agent = role is None or role.startswith('agent')
    rag = role is None or role.startswith('rag')
    if agent:
        tool_limits()
        admission_policy('agent_run')
        number('MEMORY_UNFINISHED_CAPACITY', 32, integer=True)
    if rag:
        admission_policy('rag_document'); admission_policy('search')
        number('RAG_PARSE_TIMEOUT_SECONDS', 900)
    for name, default in (('FLOW_UNAVAILABLE_GRACE_SECONDS', 30), ('PROGRESS_IDLE_SECONDS', 60), ('PROGRESS_POLL_SECONDS', 5)):
        number(name, default)
    number('DOWNSTREAM_REQUEST_RATE',1); number('DOWNSTREAM_REQUEST_BURST',2,integer=True)
    groups=json.loads(os.environ.get('DOWNSTREAM_GROUPS','{}'))
    limits=json.loads(os.environ.get('DOWNSTREAM_LIMITS','{}'))
    if not isinstance(groups,dict) or not isinstance(limits,dict): raise ValueError('Downstream maps must be JSON objects')
    if any(not isinstance(v,str) or not v for v in groups.values()): raise ValueError('Downstream group names must be nonempty strings')
    if any(not isinstance(v,dict) for v in limits.values()): raise ValueError('Downstream limit overrides must be objects')
    configured = []
    if agent: configured.extend([('agent-model','AGENT_MODEL_URL'),('embedding','AGENT_EMBEDDING_URL'),('embedding','RAG_EMBEDDING_URL'),('rag','RAG_BASE_URL')])
    if rag: configured.extend([('embedding','RAG_EMBEDDING_URL'),('rewrite','RAG_REWRITE_URL'),('rerank','RAG_RERANK_URL')])
    for service, env in configured:
        if os.environ.get(env): downstream_policy(service, os.environ[env])
