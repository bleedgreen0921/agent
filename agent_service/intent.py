"""Explicit user grants constrain side effects independently of model intent."""
from agent_service.acceptance import digest
from agent_service.tooling import load_providers

def declarations():
    return {d.tool.name:d for _,p in load_providers() for d in p.tools}

def validate_grants(grants):
    configured = declarations()
    seen = set()
    for grant in grants:
        item = grant.model_dump() if hasattr(grant, 'model_dump') else grant
        tool = configured.get(item['tool_name'])
        if tool is None or tool.kind != 'side_effect' or tool.intent_spec.operation != item['operation']:
            raise ValueError('Authorization does not name a configured side-effect operation')
        spec = tool.intent_spec
        arguments = item['exact_arguments']
        if set(arguments) != set(spec.effect_fields) or any(v is None or isinstance(v,str) and any(c in v for c in '*?') or isinstance(v,(dict,list)) for v in arguments.values()):
            raise ValueError('Authorization requires exact scalar values for every effect field')
        key=digest({k:v for k,v in item.items() if k!='max_calls'})
        if key in seen: raise ValueError('Duplicate tool authorization')
        seen.add(key)

def matching_grant(declaration, arguments, grants):
    if declaration.kind == 'read_only': return None
    for item in grants:
        grant = item['grant_data']
        if item.get('uses',0)>=grant['max_calls']: continue
        if grant['tool_name'] != declaration.tool.name or grant['operation'] != declaration.intent_spec.operation:
            continue
        if set(grant['exact_arguments']) != set(declaration.intent_spec.effect_fields): continue
        # JSON canonical equality does not equate booleans with integers.
        if all(k in arguments and digest(arguments[k]) == digest(v) for k,v in grant['exact_arguments'].items()):
            return item['id']
    return None

def batch_identity(task, contract, message, calls):
    return digest(dict(task=task, contract=contract, triggering=message.additional_kwargs.get('_execution_call'), calls=calls))
