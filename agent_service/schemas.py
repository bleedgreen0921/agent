"""Business schemas shared by Agent and Memory, independent of execution graphs."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class PlanStep(Strict):
    goal: str = Field(min_length=1)

class Plan(Strict):
    steps: list[PlanStep] = Field(min_length=1, max_length=5)

class InvalidPlanError(ValueError):
    pass

class DraftClaim(Strict):
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
