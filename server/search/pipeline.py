"""P3 检索、同经历校验与逐项判断(规格 7.3 / 8)。

三阶段:
1. 每个条件生成独立检索表达,OR 条件分别召回取并集;候选池=逐条件并集;
2. 对候选池取回经历级 chunk,语义匹配交给模型,代码负责引用存在性、同 scope、
   日期区间、角色动作与 AND/OR 组合;跨 scope 组合仅用于 CANDIDATE_ANYWHERE;
3. 输出 CriterionResult;相似度只是召回信号,不作符合证据。
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

from ..core.contracts import (ConditionNode, CriterionResult, CriterionState,
                              JobCriterion, ScopeRule)


def atomic_leaves(node: ConditionNode) -> list[ConditionNode]:
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.op in ("AND", "OR"):
            stack.extend(n.children)
        else:
            out.append(n)
    return out


def criterion_queries(c: JobCriterion) -> list[str]:
    """OR 顶层拆成多条独立查询;其余按叶子拼接。"""
    if c.expression.op == "OR":
        qs = []
        for ch in c.expression.children:
            leaves = atomic_leaves(ch)
            qs.append(" ".join(_leaf_text(l) for l in leaves))
        return [q for q in qs if q]
    return [" ".join(_leaf_text(l) for l in atomic_leaves(c.expression))]


def _leaf_text(n: ConditionNode) -> str:
    parts = [p for p in (n.predicate, n.value) if p]
    if n.examples:
        parts.append(" ".join(n.examples))
    return " ".join(parts)


def or_groups(node: ConditionNode) -> list[list[ConditionNode]]:
    if node.op == "OR":
        return [atomic_leaves(ch) for ch in node.children]
    return [atomic_leaves(node)]


def and_groups(node: ConditionNode) -> list[list[ConditionNode]]:
    if node.op == "AND":
        return [atomic_leaves(ch) for ch in node.children]
    return [atomic_leaves(node)]


# ---------------- 年限计算(规格 7.3) ----------------

_DATE_RE = re.compile(r"(20|19)\d{2}[.\-/年]?\s*(\d{1,2})?")


def parse_month(s: Optional[str]) -> Optional[tuple[int, int]]:
    if not s:
        return None
    m = re.search(r"(19|20)(\d{2})\D{0,2}(\d{1,2})?", s)
    if not m:
        return None
    year = int(m.group(0)[:4])
    month = int(m.group(3)) if m.group(3) else None
    return (year, month)


def months_between(start: Optional[tuple[int, int]], end: Optional[tuple[int, int]],
                   as_of: datetime.date) -> Optional[int]:
    """end 为 None 表示'至今',按 as_of 计算并记录;月份缺失返回 None(待确认)。"""
    if start is None or start[1] is None:
        return None
    if end is None:
        ey, em = as_of.year, as_of.month
    else:
        if end[1] is None:
            return None
        ey, em = end
    return (ey - start[0]) * 12 + (em - start[1])


def dedup_months(intervals: list[tuple[tuple[int, int], tuple[int, int]]]) -> Optional[int]:
    """重叠月份去重;任一区间的月份缺失则返回 None。"""
    months: set[tuple[int, int]] = set()
    for (sy, sm), (ey, em) in intervals:
        if sm is None or em is None:
            return None
        t = sy * 12 + sm - 1
        e = ey * 12 + em - 1
        if e < t:
            return None
        for m in range(t, e + 1):
            months.add((m // 12, m % 12 + 1))
    return len(months)


def duration_state(c: JobCriterion, scopes: list[dict], as_of: datetime.date) -> tuple[CriterionState, str]:
    """年限类条件:只累计有起止日期且属于指定能力/工作类型的区间。"""
    if c.min_duration_months is None:
        return CriterionState.SUPPORTED, "no_duration_requirement"
    intervals = []
    for sc in scopes:
        st = parse_month(sc.get("start_date"))
        en = parse_month(sc.get("end_date")) if sc.get("end_date") not in (None, "") else None
        if sc.get("end_date") in ("至今", "Present", "now", ""):
            en = None
        if st is None:
            continue
        if en is None and sc.get("end_date") not in ("至今", "Present", "now", ""):
            continue
        intervals.append((st, en or (as_of.year, as_of.month)))
    if not intervals:
        return CriterionState.NOT_EVIDENCED, "no_dated_scopes"
    total = dedup_months(intervals)
    if total is None:
        return CriterionState.NEEDS_REVIEW, "duration_months_missing"
    if total >= c.min_duration_months:
        return CriterionState.SUPPORTED, f"dedup_months={total}"
    return CriterionState.NOT_EVIDENCED, f"dedup_months={total}<{c.min_duration_months}"


# ---------------- 同经历校验 ----------------


def scopes_compatible(scope_ids: list[str], scopes_by_id: dict, rule: ScopeRule) -> bool:
    if rule == ScopeRule.CANDIDATE_ANYWHERE:
        return True
    if not scope_ids:
        return False
    kinds = {scopes_by_id[s].kind for s in scope_ids if s in scopes_by_id}
    if rule == ScopeRule.SAME_WORK:
        return len(set(scope_ids)) == 1 and kinds == {"work"}
    if rule == ScopeRule.SAME_PROJECT:
        return len(set(scope_ids)) == 1 and kinds == {"project"}
    if rule == ScopeRule.WORK_PROJECT_CHAIN:
        if len(set(scope_ids)) == 1:
            return True
        def _root(sid):
            visited = {sid}
            cur = sid
            while cur in scopes_by_id and scopes_by_id[cur].parent_scope_id:
                p = scopes_by_id[cur].parent_scope_id
                if p in visited:
                    break
                visited.add(p)
                cur = p
            return cur
        roots = {_root(s) for s in scope_ids if s in scopes_by_id}
        return len(roots) == 1
    return False


def build_result(criterion: JobCriterion, candidate_id: str, state: CriterionState,
                 fact_ids: list[str], span_ids: list[str], reason: str,
                 question: Optional[str] = None, model_version: str = "") -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        candidate_id=candidate_id,
        state=state,
        supporting_fact_ids=fact_ids,
        source_span_ids=span_ids,
        reason_code=reason,
        review_question=question,
        model_version=model_version,
        decision_version="v1",
    )
