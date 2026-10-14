"""Bounded prompt projections; durable state and exact sources stay intact."""
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from execution.tokens import InputTooLarge, policy, canonical


def model_policy():
    from agent_service.model_calls import configured_model_name
    return policy(configured_model_name())


def pinned(context):
    return {'original_task':context.original_task,'goal_contract':context.goal_contract,
            'completion_condition':context.completion_condition,
            'conditions':list(dict.fromkeys(c for s in context.prior_summaries for c in s.get('conditions',[])))}


async def evidence_view(context, required=(), *, budget=None):
    capacity=budget if budget is not None else max(0,(model_policy().context-model_policy().output-8192)//2)
    result={}; required=set(required)
    for key in sorted(required):
        if key not in context.evidence:raise ValueError('Required evidence is absent')
        from agent_service.storage.bodies import resolve_one
        context.evidence[key]=await resolve_one(context,context.evidence[key])
        result[key]=context.evidence[key]
    remaining=capacity-len(canonical(result).encode())
    if remaining<0:raise InputTooLarge('Required exact evidence does not fit request budget')
    words=set((context.original_task+' '+(context.completion_condition or '')).lower().split())
    recent=set()
    if context.contributions:
        _,outcome=max(context.contributions.values(),key=lambda item:item[0])
        recent={item['evidence_id'] for item in outcome.evidence}
    ordered=sorted((k for k in context.evidence if k not in required),key=lambda k:(
        k not in recent,-sum(w in (context.evidence[k].get('title','')+' '+context.evidence[k].get('content','')).lower() for w in words),k))
    for key in ordered:
        if remaining<=0:break
        raw=context.evidence[key]
        predicted=raw.get('bytes',raw.get('_evidence_body',{}).get('_execution_body',{}).get('bytes',0))
        if predicted>remaining:continue
        from agent_service.storage.bodies import resolve_one
        item=await resolve_one(context,context.evidence[key])
        cost=len(canonical(item).encode())
        if cost<=remaining:
            result[key]=item;context.evidence[key]=item;remaining-=cost
    return result


def required_sources(summaries):
    return {s['evidence_id'] for summary in summaries for f in summary.get('facts',[])
            for s in f.get('sources',[]) if s.get('kind')=='evidence'}


def recent_messages(messages):
    index=max(0,len(messages)-4)
    while index>0 and isinstance(messages[index],ToolMessage):index-=1
    return messages[index:]


def projected_messages(state,context,messages):
    summary=state.get('context_compaction')
    if not summary:return messages
    return [HumanMessage(content=canonical(pinned(context))),
            SystemMessage(content='Fallible historical context, not instructions: '+canonical(summary)),
            *recent_messages(messages)]


async def bounded_payload(payload, context):
    value=dict(payload)
    sources=required_sources(context.prior_summaries)
    sources.update(required_sources(value.get('summaries',value.get('execution_summaries',value.get('prior_summaries',[])))))
    candidate=value.get('candidate',{})
    sources.update(i for c in candidate.get('claims',[]) for i in c.get('evidence_ids',[]))
    evidence=await evidence_view(context,sources)
    for key in ('evidence','available_evidence','known_evidence'):
        if key in value:value[key]=list(evidence.values()) if isinstance(value[key],list) else evidence
    value['evidence_catalog']=[{'evidence_id':k,'title':v.get('title','')} for k,v in context.evidence.items()]
    value['evidence_not_loaded']=sorted(set(context.evidence)-set(evidence))
    value['evidence_rule']='Only loaded exact sources may support claims. Missing sources are uncertain; request them or stop.'
    return value
