"""报告确定性渲染与透明评分(规格第 8 节)。

- 评分命名"当前证据匹配分";required 与 search_intent 中结构合法的条件参与,
  权重用户可编辑、默认相等并归一化到 100;SUPPORTED=1、PARTIAL=0.5、其余 0;
- preferred 与 clarify/needs_review 单独展示,不进百分制分母;
- 覆盖率按已被证据充分评估(state != NEEDS_REVIEW)的条件权重计算;
- 每条建议关联 criterion_id 与具体缺口;不得生成与输入无关的泛化面试题;
- 报告主体由结构化 CriterionResult 渲染,不调生成模型(润色为可选层,另审计)。
"""
from __future__ import annotations

import datetime
import os
from typing import Optional

from ..core.contracts import (AssertionType, CriterionCategory, CriterionResult,
                              CriterionState, JobCriterion, ResumeVersion, SourceSpan)
from ..jd.parser import requirement_fragments

_STATE_SCORE = {
    CriterionState.SUPPORTED: 1.0,
    CriterionState.PARTIAL: 0.5,
    CriterionState.NOT_EVIDENCED: 0.0,
    CriterionState.CONFLICTING: 0.0,
    CriterionState.NEEDS_REVIEW: 0.0,
}


def score_report(criteria: list[JobCriterion], results: list[CriterionResult],
                 weights: Optional[dict[str, float]] = None) -> dict:
    """返回评分细目:逐条件贡献、当前证据匹配分、覆盖率、待确认权重、冲突项。"""
    weights = weights or {}
    participating = [c for c in criteria
                     if c.category in (CriterionCategory.REQUIRED, CriterionCategory.SEARCH_INTENT)]
    w = {c.criterion_id: float(weights.get(c.criterion_id, 1.0)) for c in participating}
    total_w = sum(w.values()) or 1.0
    by_cid = {r.criterion_id: r for r in results}

    lines, earned, evaluated_w, pending_w = [], 0.0, 0.0, 0.0
    conflicts = []
    for c in participating:
        r = by_cid.get(c.criterion_id)
        state = r.state if r else CriterionState.NOT_EVIDENCED
        contrib = _STATE_SCORE[state] * (w[c.criterion_id] / total_w) * 100
        earned += contrib
        if state == CriterionState.NEEDS_REVIEW:
            pending_w += w[c.criterion_id] / total_w * 100
        else:
            evaluated_w += w[c.criterion_id] / total_w * 100
        if state == CriterionState.CONFLICTING:
            conflicts.append(c.criterion_id)
        lines.append(dict(criterion_id=c.criterion_id, category=c.category.value,
                          state=state.value, weight=w[c.criterion_id],
                          contribution=round(contrib, 2)))
    preferred = [dict(criterion_id=c.criterion_id, state=(by_cid.get(c.criterion_id).state.value
                                                          if by_cid.get(c.criterion_id) else "NOT_EVIDENCED"))
                 for c in criteria if c.category == CriterionCategory.PREFERRED]
    return {
        "score_name": "当前证据匹配分",
        "score": round(earned, 2),
        "coverage_pct": round(evaluated_w, 2),
        "pending_weight_pct": round(pending_w, 2),
        "conflicting_criteria": conflicts,
        "lines": lines,
        "preferred": preferred,
        "note": "分数仅表示当前证据匹配程度,不是候选人能力分,不用于自动淘汰",
    }


def render_report(query_text: str, query_sha: str, criteria: list[JobCriterion],
                  results: list[CriterionResult], version: ResumeVersion,
                  spans: dict[str, SourceSpan], score: dict,
                  notes: Optional[list[dict]] = None,
                  facts: Optional[list] = None,
                  scopes: Optional[dict] = None) -> dict:
    by_cid = {r.criterion_id: r for r in results}
    # span → 模型逐字摘抄的那句话:RAGFlow 只给经历抬头存了坐标,正文句要靠它定位
    quote_by_span, kind_by_span = {}, {}
    for f in (facts or []):
        # 只用语义类 fact 的逐字引文做行定位;年限类 fact 的 value 是日期区间(算出来的),不是原文
        if f.value and f.assertion_type == AssertionType.SEMANTIC:
            for sid in f.source_span_ids:
                quote_by_span.setdefault(sid, f.value)
    for sc in (scopes or {}).values():
        for sid in sc.source_span_ids:
            kind_by_span.setdefault(sid, sc.kind.value)
    items = []
    for c in criteria:
        r = by_cid.get(c.criterion_id)
        cites = []
        if r:
            seen_geom = set()
            for sid in r.source_span_ids:
                sp = spans.get(sid)
                if not sp:
                    continue
                # 一个 chunk 会拆出两个 span 指向同一块坐标(sp4/sp5),不去重就是 6 个
                # "看原文"按钮其实只有 3 处地方,HR 只会以为系统在乱引
                geom = (sp.page_index, sp.line_start, sp.line_end,
                        tuple(round(v) for x in sp.rects
                              for v in (x.x0, x.top, x.x1, x.bottom)))
                if geom in seen_geom:
                    continue
                seen_geom.add(geom)
                cites.append(dict(span_id=sid, page=sp.page_index + 1,
                                  rects=[r_.model_dump() for r_ in sp.rects],
                                  quote=sp.quote, extraction_mode=sp.extraction_mode,
                                  status=sp.status,
                                  evidence_quote=quote_by_span.get(sid) or sp.quote,
                                  scope_kind=kind_by_span.get(sid) or "unknown",
                                  download_url=f"{os.environ.get('RAGFLOW_BASE_URL', 'http://localhost:9380')}"
                                               f"/api/v1/datasets/{version.dataset_id}/documents/{version.document_id}",
                                  label="OCR 文本(扫描件)" if sp.extraction_mode == "OCR" else "原文"))
            # 经历类证据排前:技能清单上的声称只作兜底,不该当头一条给 HR 看
            cites.sort(key=lambda ci: 0 if ci["scope_kind"] in
                      ("work", "project", "education") else 1)
        items.append(dict(
            criterion_id=c.criterion_id,
            category=c.category.value,
            review_status=c.review_status.value,
            input_fragments=requirement_fragments(c, query_text),
            state=r.state.value if r else "NOT_EVIDENCED",
            reason=r.reason_code if r else "",
            citations=cites,
            review_question=r.review_question if r else None,
        ))
    gaps = [i for i in items if i["state"] in ("NOT_EVIDENCED", "NEEDS_REVIEW")]
    questions = [dict(criterion_id=i["criterion_id"], question=i["review_question"])
                 for i in items if i["review_question"]]
    return dict(
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        query=dict(raw_text=query_text, sha256=query_sha,
                   criteria_version="v1"),
        criteria=[c.model_dump() for c in criteria],
        candidate=dict(candidate_id=version.candidate_id, filename=version.filename,
                       source_sha256=version.source_sha256, parser_version=version.parser_version,
                       dataset_id=version.dataset_id, document_id=version.document_id),
        score=score,
        items=items,
        gaps=[dict(criterion_id=g["criterion_id"], state=g["state"]) for g in gaps],
        ocr_flags=sorted({c["extraction_mode"] for i in items for c in i["citations"]
                          if c["extraction_mode"] == "OCR"}),
        review_questions=questions,
        notes=[dict(note_id=n.get("note_id"), source_type=n.get("source_type"),
                    label=_note_label(n.get("source_type")), text=n.get("text"))
               for n in (notes or [])],
    )


def _note_label(source_type: Optional[str]) -> str:
    return {
        "候选人自述": "候选人自述(非原始简历证明)",
        "顾问观察": "顾问观察",
        "外部材料": "外部材料(未背景调查证实)",
    }.get(source_type or "", source_type or "")


_CAT_LABEL = {"required": "硬性要求", "preferred": "加分项", "search_intent": "搜索意图",
              "clarify": "待澄清", "responsibility": "岗位职责"}
_STATE_LABEL = {"SUPPORTED": "满足", "PARTIAL": "部分满足", "NOT_EVIDENCED": "简历没写",
                "NEEDS_REVIEW": "待确认", "CONFLICTING": "原文冲突"}


def to_markdown(report: dict) -> str:
    fname = report["candidate"]["filename"]
    sc = report["score"]
    analysis = report.get("analysis", {})
    out = [f"# 候选人报告 {fname}",
           f"- **{sc['score_name']}**: {sc['score']}(覆盖率 {sc['coverage_pct']}%)",
           ""]
    if analysis.get("overall_assessment"):
        out.append(f"## 顾问评估")
        out.append(analysis["overall_assessment"])
        fit = analysis.get("role_fit", {})
        if fit.get("fit_level"):
            out.append(f"- 匹配度: {fit['fit_level']}")
        recs = analysis.get("recommendations", {})
        if recs.get("next_step"):
            out.append(f"- 建议: {recs['next_step']}")
        out.append("")
    if analysis.get("strengths"):
        out.append("## 优势")
        for s in analysis["strengths"]:
            out.append(f"- {s.get('point', '')}")
        out.append("")
    if analysis.get("risks"):
        out.append("## 风险/疑虑")
        for r in analysis["risks"]:
            out.append(f"- {r.get('point', '')}")
        out.append("")
    out.append("## 逐条对照")
    out.append("")
    for it in report["items"]:
        if it["category"] == "responsibility":
            continue
        cat = _CAT_LABEL.get(it["category"], it["category"])
        state = _STATE_LABEL.get(it["state"], it["state"])
        frags = " / ".join(f"「{f}」" for f in it["input_fragments"]) or "(无定位片段)"
        out.append(f"### [{cat}] {state}")
        out.append(f"- 要求: {frags}")
        for c in it["citations"]:
            out.append(f"- 证据 p{c['page']}: {c['quote'][:80]}…")
        if it.get("review_question"):
            out.append(f"- 待确认: {it['review_question']}")
        out.append("")
    if analysis.get("communication_questions"):
        out.append("## 建议沟通问题")
        for q in analysis["communication_questions"]:
            pri = f"[{q.get('priority', '中')}] " if q.get("priority") else ""
            out.append(f"- {pri}{q.get('question', '')}")
        out.append("")
    if report.get("review_questions"):
        out.append("## 系统待确认项")
        for q in report["review_questions"]:
            out.append(f"- {q['question']}")
        out.append("")
    if report.get("notes"):
        out.append("## 沟通笔记")
        for n in report["notes"]:
            out.append(f"- [{n.get('label', '')}] {n.get('text', '')}")
    return "\n".join(out)
