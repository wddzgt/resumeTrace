"""ResumeTrace 核心数据契约(规格第 6 节)与不变量校验。

所有报告交付前必须通过 validate_invariants();违反任一不变量抛 InvariantError
并阻止报告交付(规格:返回错误并阻止交付,不得静默)。
"""
from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class InvariantError(ValueError):
    """数据不变量违反:阻止报告交付。"""


class CriterionState(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    NOT_EVIDENCED = "NOT_EVIDENCED"
    CONFLICTING = "CONFLICTING"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ScopeKind(str, Enum):
    WORK = "work"
    PROJECT = "project"
    EDUCATION = "education"
    PROFILE = "profile"


class AssertionType(str, Enum):
    ORIGINAL = "原文直述"
    COMPUTED = "由日期计算"
    SEMANTIC = "模型语义归纳"
    COMMUNICATION = "沟通记录"


class ScopeRule(str, Enum):
    CANDIDATE_ANYWHERE = "CANDIDATE_ANYWHERE"
    SAME_WORK = "SAME_WORK"
    SAME_PROJECT = "SAME_PROJECT"
    WORK_PROJECT_CHAIN = "WORK_PROJECT_CHAIN"


class Rect(BaseModel):
    page: int = Field(ge=0)
    x0: float
    x1: float
    top: float
    bottom: float


class SourceSpan(BaseModel):
    source_sha256: str
    page_index: int = Field(ge=0)
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    rects: list[Rect] = Field(default_factory=list)
    quote: str
    quote_hash: str
    extraction_mode: str = Field(pattern="^(metadata|OCR|unknown)$")
    status: str = Field(pattern="^(located|unlocated)$")

    @model_validator(mode="after")
    def _check(self):
        if self.line_end < self.line_start:
            raise InvariantError("SourceSpan.line_end < line_start")
        if self.status == "located":
            if not self.rects:
                raise InvariantError("located span 必须有 rects")
            # 行号与页码来自同次解析:page_index 为首行所在页,多行事实允许跨页矩形
            if self.rects[0].page != self.page_index:
                raise InvariantError("行号与矩形页码必须来自同次解析")
            if any(r.page < self.page_index for r in self.rects):
                raise InvariantError("矩形页码不得早于 span 起始页")
            expect = hashlib.sha256(_norm(self.quote).encode()).hexdigest()
            if expect != self.quote_hash:
                raise InvariantError("quote 与 quote_hash 不一致")
        return self


class ExperienceScope(BaseModel):
    scope_id: str
    kind: ScopeKind
    parent_scope_id: Optional[str] = None
    organization: Optional[str] = None
    project_name: Optional[str] = None
    role: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    source_span_ids: list[str] = Field(default_factory=list)


class ResumeVersion(BaseModel):
    candidate_id: str
    dataset_id: str
    document_id: str
    source_sha256: str
    filename: str
    ingested_at: str
    parser_version: str
    status: str


class ResumeFact(BaseModel):
    fact_id: str
    candidate_id: str
    scope_id: Optional[str] = None
    predicate: str
    value: str
    assertion_type: AssertionType
    source_span_ids: list[str] = Field(default_factory=list)
    quality_flag: str = ""


class CriterionCategory(str, Enum):
    RESPONSIBILITY = "responsibility"
    REQUIRED = "required"
    PREFERRED = "preferred"
    SEARCH_INTENT = "search_intent"
    CLARIFY = "clarify"


class ReviewStatus(str, Enum):
    AUTO = "auto"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"


class ConditionNode(BaseModel):
    """AND/OR 树:op 为 None 时为原子条件。"""
    op: Optional[str] = Field(default=None, pattern="^(AND|OR)$")
    children: list["ConditionNode"] = Field(default_factory=list)
    predicate: Optional[str] = None
    value: Optional[str] = None
    examples: list[str] = Field(default_factory=list)
    input_span_offsets: list[tuple[int, int]] = Field(default_factory=list)


class JobCriterion(BaseModel):
    criterion_id: str
    query_id: str
    input_spans: list[tuple[int, int]] = Field(default_factory=list)
    category: CriterionCategory
    expression: ConditionNode
    min_duration_months: Optional[int] = None
    scope_rule: ScopeRule = ScopeRule.CANDIDATE_ANYWHERE
    weight: float = 1.0
    review_status: ReviewStatus = ReviewStatus.AUTO


class CriterionResult(BaseModel):
    criterion_id: str
    candidate_id: str
    state: CriterionState
    supporting_fact_ids: list[str] = Field(default_factory=list)
    source_span_ids: list[str] = Field(default_factory=list)
    reason_code: str = ""
    review_question: Optional[str] = None
    model_version: str = ""
    decision_version: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def validate_invariants(
    versions: list[ResumeVersion],
    spans: dict[str, SourceSpan],
    facts: list[ResumeFact],
    results: list[CriterionResult],
) -> None:
    """规格第 6 节数据不变量,任一违反抛 InvariantError。"""
    sha_by_candidate = {v.candidate_id: v.source_sha256 for v in versions}
    for sp in spans.values():
        if sp.source_sha256 not in sha_by_candidate.values():
            raise InvariantError(f"SourceSpan.source_sha256 未对应任何 ResumeVersion: {sp.source_sha256}")
    fact_by_id = {f.fact_id: f for f in facts}
    for f in facts:
        if f.candidate_id not in sha_by_candidate:
            raise InvariantError(f"ResumeFact.candidate_id 未知: {f.candidate_id}")
        for sid in f.source_span_ids:
            sp = spans.get(sid)
            if sp is None:
                raise InvariantError(f"ResumeFact 引用了不存在的 span: {sid}")
            if sp.source_sha256 != sha_by_candidate[f.candidate_id]:
                raise InvariantError("ResumeFact 的 span 不属于同一候选人")
    for r in results:
        if not r.supporting_fact_ids and r.state == CriterionState.SUPPORTED:
            raise InvariantError(f"SUPPORTED 结论必须带支持事实: {r.criterion_id}/{r.candidate_id}")
        for fid in r.supporting_fact_ids:
            if fid not in fact_by_id:
                raise InvariantError(f"CriterionResult 引用了不存在的 fact: {fid}")
        fact_spans = set()
        for fid in r.supporting_fact_ids:
            fact_spans.update(fact_by_id[fid].source_span_ids)
        if not set(r.source_span_ids) <= fact_spans:
            raise InvariantError(
                f"CriterionResult.source_span_ids 必须来自其 supporting_fact_ids: {r.criterion_id}/{r.candidate_id}")
