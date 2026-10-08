"""候选人专业分析报告生成(规格第 9 节)。

在确定性评分报告之上叠加 LLM 生成的顾问式分析:
- 优势/风险/岗位匹配度判断
- 后续沟通问题清单(针对证据缺口和模糊点)
- 录用建议与下一步行动

分析层与证据层分离:评分仍由结构化 CriterionResult 确定性计算,
分析只是对已有证据的顾问式解读,不改变分数。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger("resumetrace.analysis")


_ANALYSIS_PROMPT = """你是一位资深招聘顾问。基于以下候选人筛选报告,撰写专业分析意见。

## 岗位要求
{query_text}

## 结构化条件判定结果
{criteria_summary}

## 当前证据匹配分: {score}/{coverage_pct}% 覆盖率

## 候选人简历关键内容
{resume_content}

请输出 JSON,字段如下:
{{
  "overall_assessment": "1-2句话总结候选人与岗位的整体匹配度",
  "strengths": [
    {{"point": "优势要点", "evidence": "简历中的原文依据(简要引用)"}}
  ],
  "risks": [
    {{"point": "风险/疑虑要点", "reason": "为什么这是风险"}}
  ],
  "role_fit": {{
    "fit_level": "强匹配/中等匹配/弱匹配/不匹配",
    "explanation": "匹配度判断的理由"
  }},
  "communication_questions": [
    {{"question": "需要和候选人沟通确认的问题", "purpose": "问这个问题的目的", "priority": "高/中/低"}}
  ],
  "recommendations": {{
    "next_step": "建议进入下一轮/建议观望/建议放弃",
    "interview_focus": "面试时应重点考察的方向",
    "reference_check": "如需背景调查,应重点核实什么"
  }}
}}

规则:
1. 所有判断必须基于报告中的证据,不得脑补简历中未出现的能力
2. communication_questions 应针对:证据缺口(NOT_EVIDENCED)、待确认项(NEEDS_REVIEW)、部分支持(PARTIAL)的条件
3. 问题要具体、可操作,不要泛泛而谈(如"请介绍一下你自己")
4. strengths 和 risks 各 2-4 条,不宜过多
5. fit_level 必须与当前证据匹配分一致:
   - 分数=0 且覆盖率≤50% → fit_level 只能是"弱匹配"或"不匹配"
   - 分数>0 且<50 → fit_level 只能是"弱匹配"或"中等匹配"
   - 分数≥50 且<80 → fit_level 只能是"中等匹配"或"强匹配"
   - 分数≥80 → fit_level 只能是"强匹配"
   不得因为"业务领域接近"而给 0 分候选人标"中等匹配";缺核心证据就是弱匹配
6. next_step 必须与 fit_level 一致:强匹配→建议进入下一轮;中等匹配→建议观望/有条件进入下一轮;弱匹配→建议放弃或仅做信息补充
7. 输出 JSON,不要其他文字"""


def generate_candidate_analysis(
    query_text: str,
    criteria: list[dict],
    results: list[dict],
    score: dict,
    resume_chunks: list[dict],
    llm_call: Callable[[str, str], str],
) -> dict:
    """基于筛选报告生成候选人专业分析。

    Args:
        query_text: 原始岗位需求文本
        criteria: 结构化条件列表(JobCriterion.model_dump())
        results: 逐条件判定结果列表
        score: 评分细目(score_report 返回值)
        resume_chunks: 候选人简历的 evidence chunks(带 content_with_weight)
        llm_call: LLM 调用函数

    Returns:
        分析结果 dict,字段见 _ANALYSIS_PROMPT
    """
    criteria_summary = _build_criteria_summary(criteria, results)
    resume_content = _build_resume_content(resume_chunks)

    prompt = _ANALYSIS_PROMPT.format(
        query_text=query_text[:500],
        criteria_summary=criteria_summary,
        score=score.get("score", 0),
        coverage_pct=score.get("coverage_pct", 0),
        resume_content=resume_content,
    )

    raw = llm_call("", prompt)
    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.M)
    try:
        analysis = json.loads(cleaned)
        logger.info("Analysis generated: fit=%s strengths=%d risks=%d questions=%d",
                   analysis.get("role_fit", {}).get("fit_level"),
                   len(analysis.get("strengths", [])),
                   len(analysis.get("risks", [])),
                   len(analysis.get("communication_questions", [])))
    except json.JSONDecodeError:
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, flags=re.S)
        try:
            analysis = json.loads(m.group(0)) if m else {}
            logger.warning("Analysis JSON parse failed, used regex extraction")
        except (json.JSONDecodeError, AttributeError):
            analysis = {}
            logger.error("Analysis JSON parse completely failed")

    if not analysis:
        return {"overall_assessment": "分析生成失败,请查看结构化评分报告", "strengths": [],
                "risks": [], "role_fit": {"fit_level": "未知", "explanation": ""},
                "communication_questions": [], "recommendations": {}}

    return _validate_analysis(analysis, score)


def _build_criteria_summary(criteria: list[dict], results: list[dict]) -> str:
    by_cid = {r["criterion_id"]: r for r in results}
    lines = []
    for c in criteria:
        cid = c["criterion_id"]
        r = by_cid.get(cid, {})
        state = r.get("state", "NOT_EVIDENCED")
        leaves = _extract_leaves(c.get("expression", {}))
        desc = " 且 ".join(leaves)[:60]
        reason = r.get("reason", "")
        line = f"- [{c['category']}] {desc} → {state}"
        if reason:
            line += f" ({reason})"
        lines.append(line)
    return "\n".join(lines) if lines else "(无条件信息)"


def _extract_leaves(expr: dict) -> list[str]:
    if expr.get("op"):
        result = []
        for child in expr.get("children", []):
            result.extend(_extract_leaves(child))
        return result
    parts = [p for p in (expr.get("predicate"), expr.get("value")) if p]
    return [" ".join(parts)] if parts else []


def _build_resume_content(chunks: list[dict], max_chars: int = 3000) -> str:
    texts = []
    total = 0
    for ck in chunks:
        t = ck.get("content_with_weight") or ""
        if not t:
            continue
        scope_kind = ck.get("resume_scope_kind_kwd", "")
        if scope_kind in ("work", "project", "education"):
            truncated = t[:600]
            texts.append(f"[{scope_kind}] {truncated}")
            total += len(f"[{scope_kind}] {truncated}")
            if total >= max_chars:
                break
    if not texts:
        for ck in chunks[:5]:
            t = ck.get("content_with_weight") or ""
            if t:
                texts.append(t[:400])
    return "\n---\n".join(texts)[:max_chars] if texts else "(无简历内容)"


def _validate_analysis(analysis: dict, score: dict) -> dict:
    defaults = {
        "overall_assessment": "",
        "strengths": [],
        "risks": [],
        "role_fit": {"fit_level": "未知", "explanation": ""},
        "communication_questions": [],
        "recommendations": {"next_step": "", "interview_focus": "", "reference_check": ""},
    }
    for k, v in defaults.items():
        if k not in analysis or analysis[k] is None:
            analysis[k] = v
    if isinstance(analysis.get("strengths"), list):
        analysis["strengths"] = [s for s in analysis["strengths"]
                                 if isinstance(s, dict) and s.get("point")]
    if isinstance(analysis.get("risks"), list):
        analysis["risks"] = [r for r in analysis["risks"]
                             if isinstance(r, dict) and r.get("point")]
    if isinstance(analysis.get("communication_questions"), list):
        analysis["communication_questions"] = [
            q for q in analysis["communication_questions"]
            if isinstance(q, dict) and q.get("question")
        ]
    # 强制 fit_level 与证据匹配分对齐,防止 LLM 忽略指令
    score_val = score.get("score", 0) or 0
    coverage = score.get("coverage_pct", 0) or 0
    fit = analysis.get("role_fit", {}).get("fit_level", "")
    if score_val == 0 and coverage <= 50 and fit in ("中等匹配", "强匹配"):
        analysis["role_fit"]["fit_level"] = "弱匹配"
    elif score_val > 0 and score_val < 50 and fit in ("强匹配", "不匹配"):
        analysis["role_fit"]["fit_level"] = "弱匹配" if fit == "不匹配" else "中等匹配"
    elif 50 <= score_val < 80 and fit in ("弱匹配", "不匹配"):
        analysis["role_fit"]["fit_level"] = "中等匹配"
    elif score_val >= 80 and fit != "强匹配":
        analysis["role_fit"]["fit_level"] = "强匹配"
    # next_step 与 fit_level 对齐
    fit = analysis["role_fit"]["fit_level"]
    step = analysis.get("recommendations", {}).get("next_step", "")
    if fit in ("弱匹配", "不匹配") and step in ("建议进入下一轮",):
        analysis["recommendations"]["next_step"] = "建议放弃或仅做信息补充"
    return analysis
