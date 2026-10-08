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

**位置**: `server/search/orchestrator.py:222`

**机制**: AND 条件的多个叶子如果分别由不同经历(scope)支持,拒绝为 `SUPPORTED`,降级为 `PARTIAL`。

```python
cross_stitch = scope_constraints and len(group) > 1 and len(set(sup_scopes)) > 1
if cross_stitch:
    partial_any = True
```

**防御目标**: 防止 LLM 将候选人的不同经历拼接成虚假的"同时满足"。

**降级不等于抹掉证据**: 判定只在条件完全成立时收集 `used_spans` 的话,`PARTIAL` 报告会只剩
"未找到可定位原文",HR 反而看不出到底哪一枝有证据、哪一枝没有。所以部分成立分支同样累加
已定位 span(经历类排在技能清单之前),状态仍是 `PARTIAL`,只是引用不再丢。

## 3. 原文定位验证 (Span Location Verification)

**位置**: `server/search/orchestrator.py:98-99`

**机制**: 即使 quote 存在,如果无法定位到具体页码和坐标(`resume_span_status_kwd != "located"`),也降级为 `NEEDS_REVIEW`。

```python
elif ck.get("resume_span_status_kwd") != "located" or not span_ids:
    state = CriterionState.NEEDS_REVIEW
```

**防御目标**: 确保每条证据都能追溯到简历原件的具体位置。

**框到句子而不是框到整段**: span 的 `position_int` 覆盖**整段经历**(抬头 + 每条正文各一个矩形),
全画出来就是满屏红框,等于没指出证据。渲染证据 PNG 时分两级收窄 ——
`server/core/evidence.py`:
- 模型给了逐字摘抄句(`evidence_quote`)→ `line_rects_for_quote()` 用 pdfplumber 现算那一句的行矩形;
- 没有摘抄句(年限类条件只用得到抬头)→ `narrow_rects_to_keywords()` 在这段坐标覆盖的行里,
  只留含**条件关键词**的行;一行都不含时留经历抬头行,与左栏显示的那句保持一致。

条件关键词由 `requirement_keywords()` 生成:简历通用词(`开发/系统/模块/负责`…)一律不算命中,
否则"要求支付或风控"会因为一句"独立完成前后端开发"被圈出来 —— 那是**看起来有证据、实际无关**,
比"未找到可定位原文"更危险。同理,解析器加在 chunk 行首的英文小标题
(`Project Description/Responsibilities（…）:`)在简历原件里并不存在,展示前要去掉。

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

## 9. 岗位原文展示不截断 (Requirement Text Integrity)

**位置**: `server/jd/parser.py` 的 `snap_fragment()` / `requirement_fragments()`，`server/search/orchestrator.py` 的 `build_review_question()`

**问题**: 条件解析模型给的是字符偏移，经常偏一两个字。原文 `熟悉Spring Boot和MySQL,有支付或风控系统开发经验优先` 会被切成 `ySQL,有支付或风控系统开`：MySQL 少了开头，句子断了腰。HR 读到的岗位条件于是变成一句不存在的话。另一个老毛病是"待确认"话术直接拼表达式叶子，输出 `请确认候选人是否具备:业务领域开发经验 系统开发经验 风控系统`，跟左栏那条要求看不出任何关系。

**机制**: 展示前把切歪的边界往外推到"一条要求"的边界：

- 边界落在字母数字串或中文串中间 → 推到小句边界（标点或 `和/与/及/或` 这类并列词）；
- 片段首尾正好是标点 → 说明上半句被切了一半，再往外包一层；
- 一个片段盖到相邻两条（JD 按行或 `1) 2) 3)` 编号分条）→ 只留与原始片段重叠最多的那条；
- 整行还原带进"要求候选人"这种套话 → 只脱套话，条件本身一个字不动；
- 推完超过 44 字就不推，别把整段职责糊到一张卡上；
- 偏移整个没通过校验（`input_spans` 为空）→ 拿条件值回原文里定位那一小句，实在找不到就留空，不拿内部标签充数。

待确认话术改成点名这句原文：`「MySQL,有支付或风控系统开发经验优先」只找到部分证据……`，让"要求 / 引文 / 红框 / 待确认"四处说的是同一句话。

**防御目标**: 原文引用不完整本身就是幻觉的一种——用户会按半截句子理解岗位条件。历史报告可用 `python3 scripts/backfill_partial_citations.py --reword` 按同一套规则重算。

## 总结

ResumeTrace 的低幻觉设计遵循以下原则:

1. **LLM 只做语义判断,不做事实断言**: LLM 判断"是否支持",但必须提供可验证的 quote
2. **程序化校验兜底**: 所有 LLM 输出都经过程序化校验,不符合规则则降级
3. **确定性优先**: 评分、覆盖率等关键指标完全由结构化数据计算
4. **可追溯性**: 每条证据都能追溯到简历原件的具体位置
5. **保守原则**: 宁可标 NEEDS_REVIEW 让人工复核,也不冒险给出虚假的 SUPPORTED
6. **展示层只还原边界,不补写内容**: 截断的原文按字符边界推回完整一句,推不出来就留空,不拿内部标签或猜测填

这些机制共同确保 ResumeTrace 的报告可信、可解释、可追溯。
