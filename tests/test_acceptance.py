"""Independent judgments, exact source bindings and synthetic side effects."""
from dataclasses import replace
import json
import os
import sys
import types
from uuid import UUID
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain.tools import ToolRuntime
from execution.contracts import ExecutionContext as ToolExecutionContext
from agent_service.acceptance import normalize_draft, render_verified, validate_summary, verify_projection, digest
from agent_service.schemas import StructuredSummary
from tests.quality_stubs import quality_value

TASK='Report the retention period and its conditions.'
CONTRACT={'requirements':[{'id':'r1','description':TASK,'completion_condition':'Period and conditions reported','conditions':['Only research records']}]}
EVIDENCE={'ev1':{'evidence_id':'ev1','document_id':'d1','document_version_id':'v1','title':'Policy','content':'Only research records are retained for seven years.','source_locator':{'kind':'txt','line_start':1,'line_end':1}}}

def candidate():
    return normalize_draft({'answer':'Only research records are retained for seven years.','claims':[{'text':'Only research records are retained for seven years.','support':'evidence','evidence_ids':['ev1']}],'notices':[]})

def assessment(c=None):
    return quality_value('FinalEvaluation',dict(candidate=c or candidate(),contract=CONTRACT,evidence=EVIDENCE))

@pytest.mark.parametrize('failure',['not_met','unsupported','contradicted','insufficient','missing','quote','conditions','original'])
def test_failed_acceptance_cannot_complete(failure):
    check=assessment()
    if failure=='not_met':check['requirements'][0]['status']='not_met'
    elif failure in ('unsupported','contradicted','insufficient'):check['claims'][0]['status']=failure
    elif failure=='missing':check['claims']=[]
    elif failure=='quote':check['claims'][0]['sources'][0]['quote']='Invented'
    elif failure=='conditions':check['conditions_preserved']=False
    else:check['original_task_covered']=False
    result=render_verified(TASK,CONTRACT,EVIDENCE,candidate(),check)
    assert result.partial
    if failure in ('unsupported','contradicted','insufficient','missing','quote','conditions'):
        assert 'Only research records are retained for seven years.' not in result.draft['answer']

def test_verified_projection_is_bound_to_exact_content():
    result=render_verified(TASK,CONTRACT,EVIDENCE,candidate(),assessment())
    assert not result.partial
    verify_projection(result)
    with pytest.raises(ValueError):verify_projection(replace(result,draft={**result.draft,'answer':'All records forever'}))
    with pytest.raises(ValueError):verify_projection(result.draft)

def test_unregistered_free_answer_is_not_published():
    c=normalize_draft({'answer':'Unsupported claim hidden in raw answer','claims':[],'notices':[]})
    result=render_verified(TASK,CONTRACT,EVIDENCE,c,assessment(c))
    assert result.partial and 'Unsupported claim' not in result.draft['answer']

def test_summary_preserves_assumptions_and_exact_conditions():
    value=StructuredSummary(assumptions=['Causality not established'],conditions=['Only research records']).model_dump()
    assert validate_summary(value,TASK,EVIDENCE,CONTRACT)==value
    value['facts']=[dict(text='Forever',verification='supported',conditions=[],sources=[dict(kind='evidence',evidence_id='ev1',quote='Forever',start=0,end=7)])]
    with pytest.raises(ValueError):validate_summary(value,TASK,EVIDENCE,CONTRACT)

@pytest.mark.parametrize('unit,text',[('synthetic','Real training improves accuracy.'),('difference','Synthetic overall increased 5%.'),('causal','Synthetic treatment causes significant improvement.'),('value','Synthetic overall improved 9 percentage points.'),('chinese_value','合成实验准确率差异为9个百分点。')])
def test_experiment_rules_override_positive_model_judgment(unit,text):
    from agent_service.acceptance import experiment_failures
    e={'evidence_id':'e','content':json.dumps({'data':{'interpretation':'descriptive_single_pair','difference_unit':'percentage_points','aggregates':{'overall':{'delta_percentage_points':5}}}}),
       'source_locator':{'kind':'experiment','synthetic':True}}
    assert experiment_failures({'text':text,'evidence_ids':['e']},{'e':e})

@tool(response_format='content_and_artifact')
async def synthetic_write(target:str,content:str,runtime:ToolRuntime[ToolExecutionContext]):
    """Append research notes to a named synthetic notebook; test-only side effect."""
    from execution.contracts import ToolOutcome
    SYNTHETIC_WRITES.append((target,content))
    return 'written',ToolOutcome('written').to_artifact()

SYNTHETIC_WRITES=[]

def synthetic_provider():
    from agent_service.tooling import ToolProvider,ToolDeclaration,ToolIntentSpec
    return ToolProvider('synthetic_side_effect','1',(ToolDeclaration(synthetic_write,'1','side_effect',False,ToolIntentSpec('append',('target',))),),1)

def configure_provider(monkeypatch):
    module=types.ModuleType('synthetic_side_effect_provider');module.tools=synthetic_provider
    monkeypatch.setitem(sys.modules,module.__name__,module)
    monkeypatch.setenv('AGENT_TOOL_PROVIDERS',module.__name__+':tools')
    SYNTHETIC_WRITES.clear()

@pytest.mark.parametrize('scenario',['allowed','no_grant','wrong_target','irrelevant','excess_batch'])
async def test_synthetic_side_effect_needs_intent_and_exact_grant(monkeypatch,scenario):
    from tests.test_agent_runtime import ScriptedModel
    from tests.native_async import native
    from agent_service import execution,runtime
    from agent_service.storage.runs import create_run,get_run
    from contracts.v1 import RunCreate,ToolAuthorization
    configure_provider(monkeypatch)
    class EvaluatedModel(ScriptedModel):
        def quality_response(self,name,data,value):
            if name=='ToolBatchEvaluation' and scenario=='irrelevant':
                value['decisions'][0]['status']='deny'
            return value
    target='other' if scenario=='wrong_target' else 'research'
    calls=[dict(id='write-1',name='synthetic_write',args={'target':target,'content':'summary'})]
    if scenario=='excess_batch':calls.append(dict(id='write-2',name='synthetic_write',args={'target':target,'content':'summary2'}))
    model=EvaluatedModel(responses=[AIMessage(content='',tool_calls=calls),AIMessage(content='Research note written')])
    monkeypatch.setattr(execution,'model',lambda:model)
    monkeypatch.setattr('agent_service.model_adapter.model',lambda:model)
    grants=[] if scenario=='no_grant' else [ToolAuthorization(tool_name='synthetic_write',operation='append',exact_arguments={'target':'research'})]
    created,_=await create_run(RunCreate(task='Append a research summary to the research notebook.',tool_authorizations=grants),'synthetic')
    job=await runtime.claim_run()
    await execution.execute_run(job['run_id'],job['lease_token'])
    result=await get_run(UUID(created['run_id']))
    assert result['status']==('completed' if scenario=='allowed' else 'partial')
    assert len(SYNTHETIC_WRITES)==(1 if scenario=='allowed' else 0)
