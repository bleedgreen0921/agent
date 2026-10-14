"""Business schemas shared by Agent and Memory, independent of execution graphs."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class PlanStep(Strict):
    goal: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)

class Plan(Strict):
    steps: list[PlanStep] = Field(min_length=1, max_length=5)

class InvalidPlanError(ValueError):
    pass

class DraftClaim(Strict):
    claim_id: str | None = None
    text: str = Field(min_length=1)
    support: Literal['evidence', 'unverified']
    evidence_ids: list[str]
    reason: str | None = None

class Notice(Strict):
    code: str
    message: str

class FinalDraft(Strict):
    answer: str = Field(min_length=1)
    claims: list[DraftClaim] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    blocks: list['AnswerBlock'] = Field(default_factory=list)

class Requirement(Strict):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    completion_condition: str = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)

class GoalContract(Strict):
    requirements: list[Requirement] = Field(min_length=1, max_length=20)

class SourceRef(Strict):
    kind: Literal['evidence', 'user']
    evidence_id: str | None = None
    quote: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

class SummaryFact(Strict):
    text: str = Field(min_length=1)
    sources: list[SourceRef] = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    verification: Literal['supported', 'unverified'] = 'unverified'

class Unresolved(Strict):
    requirement_id: str
    reason: str = Field(min_length=1)

class StructuredSummary(Strict):
    facts: list[SummaryFact] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    unresolved: list[Unresolved] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

class StepEvaluation(Strict):
    status: Literal['met', 'not_met', 'uncertain']
    reason: str
    summary: StructuredSummary

class ToolDecision(Strict):
    tool_call_id: str
    status: Literal['allow', 'deny', 'uncertain']
    requirement_ids: list[str]
    reason: str

class ToolBatchEvaluation(Strict):
    decisions: list[ToolDecision]

class AnswerBlock(Strict):
    id: str = Field(min_length=1)
    kind: Literal['claim', 'deliverable']
    text: str = ''
    claim_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)

class RequirementEvaluation(Strict):
    requirement_id: str
    status: Literal['met', 'not_met', 'uncertain']
    reason: str

class ClaimEvaluation(Strict):
    claim_id: str
    status: Literal['supported', 'unsupported', 'contradicted', 'insufficient']
    sources: list[SourceRef] = Field(default_factory=list)
    reason: str

class BlockEvaluation(Strict):
    block_id: str
    status: Literal['accepted', 'rejected', 'uncertain']
    reason: str

class FinalEvaluation(Strict):
    requirements: list[RequirementEvaluation]
    claims: list[ClaimEvaluation]
    blocks: list[BlockEvaluation]
    original_task_covered: bool
    conditions_preserved: bool
    reason: str

FinalDraft.model_rebuild()
