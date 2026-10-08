"""一键搜索编排:条件解析 → 逐条件召回 → 候选人归并 → 同经历校验判断 → 报告(规格 4/7/8)。

判断原则(规格 7.3/8):
- 相似度只是召回与排序信号,不作"符合岗位"的证据;
- 模型返回支持结论必须附候选事实引用,服务端校验其 SourceSpan,无有效引用降 NEEDS_REVIEW;
- 代码只做可形式化的关系约束(scope/日期/AND-OR),不用关键词硬猜语义;
- 未见证据 ≠ 不具备;CONFLICTING 必须给出两处原文。
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Callable, Optional

from ..core.contracts import (AssertionType, CriterionCategory, CriterionResult,
                              CriterionState, JobCriterion, ResumeFact, ScopeRule,
                              _norm as _norm_ws)
from ..search.pipeline import (and_groups, build_result, criterion_queries,
                               duration_state, or_groups, parse_month,
                               scopes_compatible)

logger = logging.getLogger("resumetrace.judge")

JUDGE_PROMPT = """你是简历证据判断器。判断给定简历片段是否支持条件,只输出 JSON。

条件:{predicate}
简历片段(带经历 scope 标识):
{chunk_text}

输出格式:
{{"state": "SUPPORTED|PARTIAL|NOT_EVIDENCED|CONFLICTING",
  "quote": "片段中支持你结论的原文句子(逐字摘抄,不得改写)",
  "role_action": "片段中体现的角色动词(如 主导/负责/参与),无则空串",
  "conflict_quote": "与条件明确冲突的原文句子,无则空串"}}

规则:片段没写的能力不得脑补;角色动词与条件要求不符时最高 PARTIAL;
只写技术清单没有经历/日期支撑年限判断时 NOT_EVIDENCED。"""


def judge_leaf(leaf_text: str, chunk_text: str, llm_call: Callable[[str, str], str]) -> dict:
    out = llm_call("", JUDGE_PROMPT.format(predicate=leaf_text, chunk_text=chunk_text))
    try:
        result = json.loads(out.strip().removeprefix("```json").removesuffix("```").strip())
        logger.debug("judge_leaf: state=%s quote_len=%d", result.get("state"), len(result.get("quote", "")))
        return result
    except json.JSONDecodeError:
        logger.warning("judge_leaf: JSON parse failed, returning NEEDS_REVIEW")
        return {"state": "NEEDS_REVIEW", "quote": "", "role_action": "", "conflict_quote": ""}


def judge_criterion(c: JobCriterion, candidate_id: str, chunks: list[dict],
                    scopes_by_id: dict, llm_call: Callable[[str, str], str],
                    as_of: datetime.date, model_version: str = "",
                    scope_constraints: bool = True) -> tuple[CriterionResult, list[ResumeFact]]:
    """对单候选人单条件出 CriterionResult;chunks 为该候选人全部经历级 chunk。"""
    scope_chunks = [ck for ck in chunks if ck.get("resume_scope_id_kwd")]
    facts: list[ResumeFact] = []
    fact_seq = 0

    def leaf_supported(leaf_text: str, restrict_scope: Optional[str] = None) -> tuple[CriterionState, Optional[str], Optional[str], Optional[str]]:
        """返回 (state, scope_id, span_id, fact_quote);restrict_scope 用于同 scope 对齐重试。"""
        nonlocal fact_seq
        best = (CriterionState.NOT_EVIDENCED, None, None, None)
        rank = {CriterionState.SUPPORTED: 3, CriterionState.PARTIAL: 2,
                CriterionState.CONFLICTING: 1, CriterionState.NOT_EVIDENCED: 0,
                CriterionState.NEEDS_REVIEW: -1}
        src = ([ck for ck in scope_chunks if ck.get("resume_scope_id_kwd") == restrict_scope]
               if restrict_scope else scope_chunks)
        # 规格:概要/技能清单 chunk 只用于召回,不得单独作为条件成立证据
        exp_src = [ck for ck in src if ck.get("resume_scope_kind_kwd") in ("work", "project", "education")]
        profile_src = [ck for ck in src if ck.get("resume_scope_kind_kwd") == "profile"]
        # 词面重叠预排:每叶子最多送 2 个 chunk 给模型,控制调用量。
        # 中文按连续串+4-gram 滑窗计分,避免"风控平台"与"实时风控数据平台"零重叠。
        def _overlap_score(leaf: str, ct: str) -> int:
            score = len(set(re.findall(r"[A-Za-z0-9+#]+", leaf))
                        & set(re.findall(r"[A-Za-z0-9+#]+", ct)))
            for run in re.findall(r"[一-鿿]+", leaf):
                if run in ct:
                    score += 2
                    continue
                grams = {run[i:i + 2] for i in range(len(run) - 1)} if len(run) >= 2 else {run}
                score += sum(1 for g in grams if g and g in ct)
            return score

        ranked = sorted(exp_src, key=lambda c2: _overlap_score(leaf_text, c2.get("content_with_weight") or ""), reverse=True)
        sent = 0
        for ck in ranked:
            text = (ck.get("content_with_weight") or "")[:1500]
            if _overlap_score(leaf_text, text) == 0 and sent >= 1:
                continue
            sent += 1
            verdict = judge_leaf(leaf_text, text, llm_call)
            state = CriterionState(verdict.get("state")) \
                if verdict.get("state") in {s.value for s in CriterionState} else CriterionState.NEEDS_REVIEW
            quote = verdict.get("quote", "")
            scope_id = ck.get("resume_scope_id_kwd")
            span_ids = ck.get("resume_source_ref_id_kwd") or []
            # 引用审计:quote 必须逐字出现在片段中,否则降 NEEDS_REVIEW(规格 7.3)
            if state in (CriterionState.SUPPORTED, CriterionState.PARTIAL):
                if not quote or quote not in text:
                    state = CriterionState.NEEDS_REVIEW
                elif ck.get("resume_span_status_kwd") != "located" or not span_ids:
                    state = CriterionState.NEEDS_REVIEW  # 无法定位原文不得作明确支持
            if state in (CriterionState.SUPPORTED, CriterionState.PARTIAL):
                fact_seq += 1
                facts.append(ResumeFact(
                    fact_id=f"{candidate_id}-{c.criterion_id}-f{fact_seq}",
                    candidate_id=candidate_id,
                    scope_id=scope_id,
                    predicate=leaf_text[:60],
                    value=quote[:200],
                    assertion_type=AssertionType.SEMANTIC,
                    source_span_ids=list(span_ids),
                    quality_flag=verdict.get("role_action", ""),
                ))
                return (state, scope_id, span_ids[0] if span_ids else None, quote)
            if rank[state] > rank[best[0]]:
                best = (state, scope_id, span_ids[0] if span_ids else None, quote)
            if sent >= 2:
                break
        # 声称只出现在概要/技能清单:不得作成立证据,但提示待确认(规格 8/ CV-02 类)
        if best[0] == CriterionState.NOT_EVIDENCED and profile_src:
            pk = max(profile_src, key=lambda c2: _overlap_score(leaf_text, c2.get("content_with_weight") or ""))
            if _overlap_score(leaf_text, pk.get("content_with_weight") or "") > 0:
                v2 = judge_leaf(leaf_text, (pk.get("content_with_weight") or "")[:1500], llm_call)
                if v2.get("state") in ("SUPPORTED", "PARTIAL"):
                    best = (CriterionState.NEEDS_REVIEW, None, None, None)
        return best

    # 年限类条件走确定性日期计算:先用模型筛出属于该条件领域的工作/项目 scope,
    # 再对选中 scope 的起止日期做去重累计(代码不算语义,只算区间)
    if c.min_duration_months is not None and c.expression.op is None \
            and any(k in (c.expression.predicate or "") + (c.expression.value or "")
                    for k in ("年", "年限", "经验")):
        from ..core.contracts import ScopeKind
        scope_dicts = []
        span_ids = []
        # 领域判定看"抬头+该 scope 描述文本",信息量足够才稳定
        scope_text: dict = {}
        for ck in scope_chunks:
            sid = ck.get("resume_scope_id_kwd")
            if sid and sid not in scope_text:
                scope_text[sid] = (ck.get("content_with_weight") or "")[:400]
        for s in scopes_by_id.values():
            if s.kind not in (ScopeKind.WORK, ScopeKind.PROJECT):
                continue
            header = " ".join(x for x in (s.organization, s.role, s.project_name,
                                          s.start_date, s.end_date) if x)
            judge_text = f"{header} {scope_text.get(s.scope_id, '')}".strip() or header
            domain_q = re.sub(r"\d+\s*年(以上|或|~|-)?", "", _brief(c)).strip()
            v = judge_leaf(domain_q or _brief(c), judge_text, llm_call)
            # 领域判定放宽:只要模型说 SUPPORTED/PARTIAL 即接受,不做引用审计
            # (领域判定只决定"哪些经历属于该能力范畴",年限由日期计算,不依赖引文)
            if v.get("state") in ("SUPPORTED", "PARTIAL") and header:
                scope_dicts.append(s.model_dump())
                span_ids.extend(s.source_span_ids)
                fact_seq += 1
                fid = f"{candidate_id}-{c.criterion_id}-f{fact_seq}"
                facts.append(ResumeFact(
                    fact_id=fid, candidate_id=candidate_id, scope_id=s.scope_id,
                    predicate="duration", value=f"{s.start_date or '?'}~{s.end_date or '至今'}",
                    assertion_type=AssertionType.COMPUTED,
                    source_span_ids=list(s.source_span_ids)))
        dated = [sd for sd in scope_dicts if parse_month(sd.get("start_date"))]
        if scope_dicts and not dated:
            state, reason = CriterionState.NEEDS_REVIEW, "duration_months_missing"
        else:
            state, reason = duration_state(c, scope_dicts, as_of)
        fact_ids = [f.fact_id for f in facts if f.fact_id.startswith(f"{candidate_id}-{c.criterion_id}-")]
        return build_result(c, candidate_id, state, fact_ids,
                            span_ids if state in (CriterionState.SUPPORTED, CriterionState.PARTIAL) else [],
                            reason,
                            question="请确认其经历分别对应哪些项目与月份"
                            if state == CriterionState.NEEDS_REVIEW else None,
                            model_version=model_version), facts

    # 语义条件:OR 组任一组内 AND 叶子全支持;组内叶子须满足 scope_rule
    satisfied_groups, partial_any, need_review_any = 0, False, False
    used_scopes, used_spans = [], []
    ors = or_groups(c.expression)
    for group in ors:
        res = []
        for leaf in group:
            leaf_text = " ".join(p for p in (leaf.predicate, leaf.value) if p)
            if leaf.examples:
                leaf_text += "(示例:" + "、".join(leaf.examples) + ")"
            state, scope_id, span_id, quote = leaf_supported(leaf_text)
            res.append([leaf, leaf_text, state, scope_id, span_id])
        # 同 scope 对齐重试:AND 叶子各自支持但落在不同经历时,尝试把各叶对齐到同一锚 scope
        if scope_constraints and len(res) > 1 \
                and all(r[2] in (CriterionState.SUPPORTED, CriterionState.PARTIAL) for r in res) \
                and len({r[3] for r in res if r[3]}) > 1:
            for anchor in dict.fromkeys(r[3] for r in res if r[3]):
                ok_all = True
                for r in res:
                    if r[3] == anchor:
                        continue
                    st2, sc2, sp2, _q = leaf_supported(r[1], restrict_scope=anchor)
                    if st2 in (CriterionState.SUPPORTED, CriterionState.PARTIAL):
                        r[2], r[3], r[4] = st2, sc2, sp2
                    else:
                        ok_all = False
                        break
                if ok_all:
                    break
        group_states = [r[2] for r in res]
        group_scopes = [r[3] for r in res if r[2] in (CriterionState.SUPPORTED, CriterionState.PARTIAL)]
        group_spans = [r[4] for r in res if r[2] in (CriterionState.SUPPORTED, CriterionState.PARTIAL)]
        if all(s == CriterionState.SUPPORTED for s in group_states) and group_states:
            sup_scopes = [s for s in group_scopes if s]
            # 同条件内 AND 叶子必须由同一经历支持(规格 7.3 同 scope 检查);
            # 不同经历分别支持不同叶子=跨经历拼接,B1 拒绝、B0(scope_constraints=False)不检查
            cross_stitch = scope_constraints and len(group) > 1 and len(set(sup_scopes)) > 1
            if cross_stitch:
                partial_any = True
            elif scopes_compatible(sup_scopes, scopes_by_id, c.scope_rule):
                satisfied_groups += 1
                used_scopes += group_scopes
                used_spans += [s for s in group_spans if s]
            else:
                partial_any = True  # 证据存在但关系约束不满足,不成立
        elif any(s in (CriterionState.SUPPORTED, CriterionState.PARTIAL) for s in group_states):
            partial_any = True
        if any(s == CriterionState.NEEDS_REVIEW for s in group_states):
            need_review_any = True

    if c.expression.op == "OR":
        ok = satisfied_groups >= 1
    else:
        ok = satisfied_groups == len(ors) and ors != []

    if ok:
        state = CriterionState.SUPPORTED
        reason = "scope_verified"
    elif partial_any:
        state = CriterionState.PARTIAL
        reason = "partial_or_cross_scope"
    elif need_review_any:
        state = CriterionState.NEEDS_REVIEW
        reason = "evidence_unlocated_or_unaudited"
    else:
        state = CriterionState.NOT_EVIDENCED
        reason = "no_evidence_in_resume"

    question = None
    if state in (CriterionState.PARTIAL, CriterionState.NOT_EVIDENCED):
        question = f"请确认候选人是否具备:{_brief(c)};如具备请提供对应项目与时间"
    elif state == CriterionState.NEEDS_REVIEW:
        question = f"证据定位或引用审计未通过,请人工复核:{_brief(c)}"
    logger.debug("judge_criterion: candidate=%s criterion=%s state=%s reason=%s",
                candidate_id, c.criterion_id, state.value, reason)
    return (build_result(c, candidate_id, state,
                         [f.fact_id for f in facts],
                         [s for s in used_spans if s], reason,
                         question=question, model_version=model_version), facts)


def _brief(c: JobCriterion) -> str:
    leaves = []
    stack = [c.expression]
    while stack:
        n = stack.pop()
        if n.op:
            stack.extend(n.children)
        else:
            leaves.append(" ".join(p for p in (n.predicate, n.value) if p))
    return " 且 ".join(leaves)[:80]
