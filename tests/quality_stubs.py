"""Scripted evaluation responses for tests only, never production judges."""
import json

QUALITY_SCHEMAS={'GoalContract','StepEvaluation','ToolBatchEvaluation','FinalEvaluation'}

def payload(messages):
    last=messages[-1]
    text=last.content if hasattr(last,'content') else last[1]
    return json.loads(text)

def quality_value(name,data):
    if name=='GoalContract':
        return {'requirements':[{'id':'r1','description':data['original_task'],'completion_condition':'Requested deliverable present','conditions':[]}]}
    if name=='ToolBatchEvaluation':
        return {'decisions':[{'tool_call_id':c['id'],'status':'allow','requirement_ids':['r1'],'reason':'Scripted relevant action'} for c in data['calls']]}
    if name=='StepEvaluation':
        outputs=[o['content'] for o in data['observations'] if o['role']=='ai' and isinstance(o['content'],str) and o['content']]
        return {'status':'met','reason':'Scripted completed step','summary':{'facts':[],'assumptions':[],'conditions':[],'unresolved':[],'outputs':outputs[-1:]}}
    candidate=data['candidate']
    claims=[]
    for c in candidate['claims']:
        sources=[]
        for eid in c['evidence_ids']:
            if eid in data['evidence']:
                text=data['evidence'][eid]['content']
                sources.append(dict(kind='evidence',evidence_id=eid,quote=text,start=0,end=len(text)))
        claims.append(dict(claim_id=c['claim_id'],status='supported' if sources else 'insufficient',sources=sources,reason='Scripted claim check'))
    return {'requirements':[dict(requirement_id=r['id'],status='met',reason='Scripted delivery check') for r in data['contract']['requirements']],
            'claims':claims,'blocks':[dict(block_id=b['id'],status='accepted',reason='Scripted block check') for b in candidate['blocks']],
            'original_task_covered':True,'conditions_preserved':True,'reason':'Scripted final check'}
