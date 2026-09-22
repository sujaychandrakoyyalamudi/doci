from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateCase(StrictModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=10, max_length=10000)
    category: Literal[
        "Document review", "Customer onboarding", "Transaction dispute", "Policy exception"
    ] = "Document review"
    priority: Literal["low", "medium", "high"] = "medium"


class ApprovalInput(StrictModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(min_length=3, max_length=2000)
    proposal_hash: str = Field(min_length=64, max_length=64)


class Evidence(BaseModel):
    chunk_id: str
    document_id: str
    filename: str
    page: int
    quote: str
    kind: Literal["evidence", "policy"]
    score: float


class Finding(BaseModel):
    statement: str = Field(max_length=1500)
    citation_ids: list[str] = Field(min_length=1, max_length=8)


class Analysis(BaseModel):
    summary: str = Field(max_length=3000)
    findings: list[Finding] = Field(max_length=12)
    action: Literal["record_resolution", "request_information", "escalate"]
    action_title: str = Field(max_length=200)
    rationale: str = Field(max_length=3000)
    confidence: float = Field(ge=0, le=1)
    missing_information: list[str] = Field(max_length=12)


class Review(BaseModel):
    verdict: Literal["pass", "revise", "escalate"]
    reasoning: str = Field(max_length=3000)
    concerns: list[str] = Field(max_length=12)
