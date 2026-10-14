"""Pure acceptance rules and rendering. No I/O, model calls or authority grants."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from agent_service.schemas import FinalDraft, FinalEvaluation, GoalContract, StructuredSummary

POLICY_VERSION = 1

def public_issue(code):
    return {
        'GOAL_ACCEPTANCE_UNAVAILABLE':'任务目标尚未核实',
        'FINAL_DRAFT_INVALID':'候选交付内容未通过检查',
        'VERIFICATION_COVERAGE_INVALID':'验收结果未完整覆盖交付内容',
        'ORIGINAL_TASK_INCOMPLETE':'尚未覆盖全部原始需求',
        'CONDITIONS_NOT_PRESERVED':'适用条件未完整保留',
        'CLAIM_NOT_SUPPORTED':'部分结论缺少证据支持',
        'CLAIM_SOURCE_INVALID':'部分引用无法核实',
        'CLAIM_SOURCE_INCOMPLETE':'部分引用缺少支持片段',
        'INVALID_EVIDENCE_REFERENCE':'部分引用未从本任务获得',
        'UNVERIFIED_CLAIM_INVALID':'部分未核实结论缺少充分说明',
        'EXPERIMENT_SYNTHETIC_UNLABELLED':'模拟实验结果未明确标注',
        'EXPERIMENT_INFERENCE_EXCEEDED':'实验结论超出了数据支持范围',
        'EXPERIMENT_UNIT_MISMATCH':'实验结果的单位不一致',
        'EXPERIMENT_VALUE_MISMATCH':'实验结论的数值与计算结果不一致',
        'DELIVERY_BLOCK_NOT_ACCEPTED':'部分交付内容未通过验收',
        'DELIVERY_CLAIM_INVALID':'部分结论未形成可核实的交付',
        'DELIVERY_BLOCK_INVALID':'部分交付内容不完整',
        'CLAIM_NOT_DELIVERED':'部分结论尚未交付',
        'REQUIREMENTS_NOT_MET':'部分需求尚未完成',
        'NO_ACCEPTED_DELIVERY':'暂无通过验收的交付内容',
        'EXECUTION_PARTIAL':'执行尚未全部完成',
        'TOOL_NOT_AUTHORIZED':'未提供该副作用动作的明确授权',
        'TOOL_AUTHORIZATION_EXHAUSTED':'工具动作超过了本任务的授权次数',
        'TOOL_INTENT_REJECTED':'工具动作未通过任务相关性检查',
        'TOOL_ARGUMENTS_INVALID':'工具参数不符合要求',
        'TOOL_NOT_DECLARED':'所请求的工具未配置',
    }.get(code,'部分内容的验收尚未完成')

def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def unique(values):
    return len(values) == len(set(values))

def validate_contract(contract):
    parsed = GoalContract.model_validate(contract)
    if not unique([r.id for r in parsed.requirements]):
        raise ValueError('Duplicate requirement ID')
    return parsed.model_dump()

def validate_plan(steps, contract):
    ids = {r['id'] for r in contract['requirements']}
    covered = set()
    for step in steps:
        references = step['requirement_ids']
        if not references or not unique(references) or not set(references) <= ids:
            raise ValueError('Invalid step requirement references')
        covered.update(references)
    if covered != ids:
        raise ValueError('Plan does not cover original requirements')

def source_valid(source, task, evidence):
    if source['kind'] == 'user':
        if source.get('evidence_id') is not None:
            return False
        content = task
    else:
        item = evidence.get(source.get('evidence_id'))
        if item is None:
            return False
        content = item['content']
    start, end = source['start'], source['end']
    return 0 <= start < end <= len(content) and content[start:end] == source['quote']

def validate_summary(summary, task, evidence, contract):
    parsed = StructuredSummary.model_validate(summary).model_dump()
    ids = {r['id'] for r in contract['requirements']}
    if any(u['requirement_id'] not in ids for u in parsed['unresolved']):
        raise ValueError('Unknown unresolved requirement')
    if any(not source_valid(s, task, evidence) for f in parsed['facts'] for s in f['sources']):
        raise ValueError('Summary has invalid source references')
    return parsed

def normalize_draft(draft):
    value = FinalDraft.model_validate(draft).model_dump()
    for i, claim in enumerate(value['claims'], 1):
        claim['claim_id'] = claim['claim_id'] or f'claim-{i}'
    if not unique([c['claim_id'] for c in value['claims']]):
        raise ValueError('Duplicate claim ID')
    if not value['blocks']:
        value['blocks'] = [dict(id=f'block-{i}', kind='claim', text='', claim_ids=[c['claim_id']], requirement_ids=[])
                           for i, c in enumerate(value['claims'], 1)]
    if not unique([b['id'] for b in value['blocks']]):
        raise ValueError('Duplicate answer block ID')
    return value

def experiment_failures(claim, evidence):
    """Conservative checks of known experimental semantics, never evaluate code."""
    items = [evidence[e] for e in claim['evidence_ids'] if e in evidence]
    experiments = [e for e in items if e['source_locator']['kind'] == 'experiment']
    errors = []
    text = claim['text'].lower()
    if any(e['source_locator']['synthetic'] for e in experiments) and not re.search(r'simulat|synthetic|模拟|合成', text):
        errors.append('EXPERIMENT_SYNTHETIC_UNLABELLED')
    for item in experiments:
        try:
            data = json.loads(item['content']).get('data', {})
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(data,dict): continue
        if data.get('interpretation') == 'descriptive_single_pair':
            if re.search(r'\bcaus(?:e|al)|\bsignificant|导致|因果|显著', text) and not re.search(r'not|cannot|no |不能|不代表|未证明|无法', text):
                errors.append('EXPERIMENT_INFERENCE_EXCEEDED')
        if data.get('difference_unit') == 'percentage_points' and re.search(r'\d\s*%|百分比', text) and not re.search(r'percentage points|百分点', text):
            errors.append('EXPERIMENT_UNIT_MISMATCH')
        # Check declared differences even when prose uses Chinese metric names.
        if data.get('difference_unit')=='percentage_points':
            aggregates=[a for a in data.get('aggregates',{}).values() if isinstance(a,dict)]
            rows=[a for a in data.get('by_snr',[]) if isinstance(a,dict)]
            values=[a['delta_percentage_points'] for a in [*aggregates,*rows] if isinstance(a.get('delta_percentage_points'),(int,float))]
            for match in re.finditer(r'([-+]?\d+(?:\.\d+)?)\s*(?:percentage points|个百分点|个?百分点)',text):
                number=float(match.group(1))
                if number>0 and re.search(r'decreas|declin|下降|降低',text): number=-number
                precision=len(match.group(1).partition('.')[2])
                tolerance=.5*10**(-precision)+1e-6 if re.search(r'about|approx|约',text) else 1e-6
                if values and not any(abs(number-v)<=tolerance for v in values): errors.append('EXPERIMENT_VALUE_MISMATCH')
        # Explicit scope names also require the specific aggregate to match.
        for key, aggregate in data.get('aggregates', {}).items():
            if key in text and isinstance(aggregate, dict):
                actual = aggregate.get('delta_percentage_points')
                match = re.search(r'([-+]?\d+(?:\.\d+)?)\s*(?:percentage points|个百分点|百分点)', text)
                if actual is not None and match and abs(float(match.group(1)) - actual) > 1e-6:
                    errors.append('EXPERIMENT_VALUE_MISMATCH')
    return errors

@dataclass(frozen=True)
class VerifiedResult:
    draft: dict
    proof: dict
    partial: bool

def render_verified(task, contract, evidence, candidate, evaluation, *, prior_partial=False, call_id=None, unavailable=None):
    candidate = normalize_draft(candidate)
    requirement_ids = {r['id'] for r in contract['requirements']}
    claim_ids = {c['claim_id'] for c in candidate['claims']}
    block_ids = {b['id'] for b in candidate['blocks']}
    issues = []
    evaluation = FinalEvaluation.model_validate(evaluation).model_dump() if evaluation is not None else None
    if evaluation is not None:
        for name, field, expected in [('requirements','requirement_id',requirement_ids),('claims','claim_id',claim_ids),('blocks','block_id',block_ids)]:
            actual = [x[field] for x in evaluation[name]]
            if not unique(actual) or set(actual) != expected:
                issues.append('VERIFICATION_COVERAGE_INVALID')
        if not evaluation['original_task_covered']: issues.append('ORIGINAL_TASK_INCOMPLETE')
        if not evaluation['conditions_preserved']: issues.append('CONDITIONS_NOT_PRESERVED')
    else:
        issues.append(unavailable or 'VERIFICATION_UNAVAILABLE')
    # Invalid coverage cannot authorize even a subset of this answer.
    valid_coverage = evaluation is not None and 'VERIFICATION_COVERAGE_INVALID' not in issues and evaluation['conditions_preserved']
    verdicts = {c['claim_id']: c for c in evaluation['claims']} if valid_coverage else {}
    blocks = {b['block_id']: b for b in evaluation['blocks']} if valid_coverage else {}
    retained, accepted, claim_reports = [], {}, []
    for claim in candidate['claims']:
        verdict = verdicts.get(claim['claim_id'])
        errors = experiment_failures(claim, evidence)
        if any(e not in evidence for e in claim['evidence_ids']): errors.append('INVALID_EVIDENCE_REFERENCE')
        if claim['support'] == 'evidence':
            if not claim['evidence_ids'] or not verdict or verdict['status'] != 'supported': errors.append('CLAIM_NOT_SUPPORTED')
            sources = verdict['sources'] if verdict else []
            if not sources or any(s['kind'] != 'evidence' or s['evidence_id'] not in claim['evidence_ids'] or not source_valid(s, task, evidence) for s in sources):
                errors.append('CLAIM_SOURCE_INVALID')
            if {s['evidence_id'] for s in sources} != set(claim['evidence_ids']): errors.append('CLAIM_SOURCE_INCOMPLETE')
        elif not claim.get('reason') or claim['evidence_ids'] or not verdict or verdict['status'] in ('unsupported','contradicted'):
            errors.append('UNVERIFIED_CLAIM_INVALID')
        if errors:
            issues.extend(errors)
        else:
            accepted[claim['claim_id']] = claim
        status=verdict['status'] if verdict else 'insufficient'
        if errors and status not in ('contradicted','unsupported'): status='unsupported'
        claim_reports.append(dict(claim_id=claim['claim_id'],text=claim['text'],status=status,
                                  reason='; '.join(dict.fromkeys(errors)) if errors else verdict['reason']))
    lines, used = [], set()
    for block in candidate['blocks']:
        decision = blocks.get(block['id'])
        if not decision or decision['status'] != 'accepted' or not set(block['requirement_ids']) <= requirement_ids:
            issues.append('DELIVERY_BLOCK_NOT_ACCEPTED'); continue
        if block['kind'] == 'claim':
            if not block['claim_ids'] or not unique(block['claim_ids']) or any(c not in accepted for c in block['claim_ids']) or block['text']:
                issues.append('DELIVERY_CLAIM_INVALID'); continue
            for cid in block['claim_ids']:
                c = accepted[cid]
                if cid in used: continue
                lines.append(c['text'] if c['support']=='evidence' else f"[Unverified / 未核实] {c['text']} ({c['reason']})")
                used.add(cid)
        elif block['text'] and not block['claim_ids'] and block['requirement_ids']:
            lines.append(block['text'])
        else:
            issues.append('DELIVERY_BLOCK_INVALID')
    if used != set(accepted): issues.append('CLAIM_NOT_DELIVERED')
    retained = [{k:v for k,v in c.items() if k != 'claim_id'} for c in candidate['claims'] if c['claim_id'] in used]
    unmet = [x for x in evaluation['requirements'] if x['status']!='met'] if valid_coverage else [dict(requirement_id=r['id'],status='uncertain',reason='Acceptance unavailable') for r in contract['requirements']]
    if unmet: issues.append('REQUIREMENTS_NOT_MET')
    if not lines: issues.append('NO_ACCEPTED_DELIVERY')
    if prior_partial: issues.append('EXECUTION_PARTIAL')
    partial = bool(issues)
    descriptions={r['id']:r['description'] for r in contract['requirements']}
    if partial:
        lines.append('Partial / 部分完成：' + '；'.join(dict.fromkeys(public_issue(i) for i in issues)))
        lines.extend(f"{descriptions[x['requirement_id']]}: {x['reason']}" for x in unmet)
    report = {'policy_version': POLICY_VERSION, 'status': 'partial' if partial else 'accepted',
              'requirements': [{**r,'description':descriptions[r['requirement_id']]} for r in (evaluation['requirements'] if valid_coverage else unmet)],
              'claims': claim_reports, 'issues': list(dict.fromkeys(issues))}
    ids = list(dict.fromkeys(e for c in retained for e in c['evidence_ids']))
    draft = dict(answer='\n\n'.join(lines), claims=retained, citations=[evidence[e] for e in ids],
                 notices=[*candidate['notices'], *([dict(code='ACCEPTANCE_PARTIAL',message='Some requirements or conclusions did not pass acceptance')] if partial else [])], acceptance=report)
    inputs = dict(task=task, contract=contract, evidence=evidence, candidate=candidate)
    proof = dict(inputs=inputs, evaluation=evaluation, call_id=str(call_id) if call_id else None,
                 prior_partial=prior_partial, unavailable=unavailable, input_hash=digest(inputs), rendered_hash=digest(draft), policy_version=POLICY_VERSION)
    return VerifiedResult(draft, proof, partial)

def verify_projection(result):
    if not isinstance(result, VerifiedResult):
        raise ValueError('Publication requires independently checked result')
    p=result.proof
    expected=render_verified(**p['inputs'], evaluation=p['evaluation'], prior_partial=p['prior_partial'], call_id=p['call_id'], unavailable=p['unavailable'])
    if expected != result:
        raise ValueError('Verified result content changed')
