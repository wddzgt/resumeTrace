# ResumeTrace 低幻觉防御机制

ResumeTrace 通过多层防御机制确保报告的低幻觉性和确定性:

## 1. 引用审计 (Quote Audit)

**位置**: `server/search/orchestrator.py:94-99`

**机制**: LLM 返回的 `quote` 必须逐字出现在原始 chunk 文本中,否则降级为 `NEEDS_REVIEW`。

```python
if state in (CriterionState.SUPPORTED, CriterionState.PARTIAL):
    if not quote or quote not in text:
        state = CriterionState.NEEDS_REVIEW
```

**防御目标**: 防止 LLM 编造不存在的原文引用。

## 2. 跨经历拼接检测 (Cross-Stitch Detection)

**位置**: `server/search/orchestrator.py:209-211`

**机制**: AND 条件的多个叶子如果分别由不同经历(scope)支持,拒绝为 `SUPPORTED`,降级为 `PARTIAL`。

```python
cross_stitch = scope_constraints and len(group) > 1 and len(set(sup_scopes)) > 1
if cross_stitch:
    partial_any = True
```

**防御目标**: 防止 LLM 将候选人的不同经历拼接成虚假的"同时满足"。

## 3. 原文定位验证 (Span Location Verification)

**位置**: `server/search/orchestrator.py:98-99`

**机制**: 即使 quote 存在,如果无法定位到具体页码和坐标(`resume_span_status_kwd != "located"`),也降级为 `NEEDS_REVIEW`。

```python
elif ck.get("resume_span_status_kwd") != "located" or not span_ids:
    state = CriterionState.NEEDS_REVIEW
```

**防御目标**: 确保每条证据都能追溯到简历原件的具体位置。

## 4. 不变量校验 (Invariant Validation)

**位置**: `server/core/contracts.py:validate_invariants()`

**机制**: 报告生成前强制校验:
- SUPPORTED 状态必须有至少一个 source_span_id
- 每个 fact 的 source_span_ids 必须指向真实存在的 span
- 每个 result 的 fact_ids 必须指向真实存在的 fact

**防御目标**: 确保数据结构的完整性和一致性。

## 5. 评分-匹配度对齐 (Score-Fit Alignment)

**位置**: `server/report/analysis.py:191-205`

**机制**: 强制 LLM 生成的 `fit_level` 与确定性评分对齐:
- 分数=0 且覆盖率≤50% → 只能是"弱匹配"或"不匹配"
- 分数≥80 → 只能是"强匹配"
- 如果 LLM 输出不一致,程序化覆盖

```python
if score_val == 0 and coverage <= 50 and fit in ("中等匹配", "强匹配"):
    analysis["role_fit"]["fit_level"] = "弱匹配"
```

**防御目标**: 防止 LLM 在分析层"圆场"或过度乐观。

## 6. 概要/技能清单降级 (Profile/Summary Downgrade)

**位置**: `server/search/orchestrator.py:117-123`

**机制**: 如果证据只出现在简历的概要或技能清单部分(而非具体工作经历),即使 LLM 判定为 SUPPORTED,也降级为 `NEEDS_REVIEW`。

```python
if best[0] == CriterionState.NOT_EVIDENCED and profile_src:
    # ... check profile chunks ...
    if v2.get("state") in ("SUPPORTED", "PARTIAL"):
        best = (CriterionState.NEEDS_REVIEW, None, None, None)
```

**防御目标**: 防止"技能清单写了=实际做过"的幻觉。

## 7. 确定性评分 (Deterministic Scoring)

**位置**: `server/report/render.py:score_report()`

**机制**: 评分完全由结构化数据计算,不依赖 LLM:
- SUPPORTED = 1.0 × 权重
- PARTIAL = 0.5 × 权重
- 其他 = 0

**防御目标**: 确保评分的可重复性和可解释性。

## 8. 领域判定宽松化 (Domain Judgment Relaxation)

**位置**: `server/search/orchestrator.py:147-150`

**机制**: 年限类条件的领域判定(判断某段经历是否属于该能力范畴)不做引用审计,因为年限由日期计算,不依赖引文。

```python
# 领域判定放宽:只要模型说 SUPPORTED/PARTIAL 即接受,不做引用审计
if v.get("state") in ("SUPPORTED", "PARTIAL") and header:
    scope_dicts.append(s.model_dump())
```

**防御目标**: 在保持严格性的同时避免过度拒绝。

## 总结

ResumeTrace 的低幻觉设计遵循以下原则:

1. **LLM 只做语义判断,不做事实断言**: LLM 判断"是否支持",但必须提供可验证的 quote
2. **程序化校验兜底**: 所有 LLM 输出都经过程序化校验,不符合规则则降级
3. **确定性优先**: 评分、覆盖率等关键指标完全由结构化数据计算
4. **可追溯性**: 每条证据都能追溯到简历原件的具体位置
5. **保守原则**: 宁可标 NEEDS_REVIEW 让人工复核,也不冒险给出虚假的 SUPPORTED

这些机制共同确保 ResumeTrace 的报告可信、可解释、可追溯。
