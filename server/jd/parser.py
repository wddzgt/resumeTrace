"""JD / 自然语言 → JobCriterion[] 条件解析(规格 7.2)。

规则:
- 保留原始文本与版本哈希;LLM 输出经 JSON Schema 校验类型/年限/逻辑运算符/输入片段偏移;
- 无法定位或关键歧义的条件标 needs_review:可参与宽召回,不作硬条件、不计证据分;
- 解析器必须区分:职责 vs 任职要求、必需 vs 优先、技术示例 vs 逻辑 OR、总年限 vs 特定经历年限;
- 不得把示例/加分/职责升级为硬门槛(规格第 0.3 条)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Callable, Optional

from ..core.contracts import (ConditionNode, CriterionCategory, JobCriterion,
                              ReviewStatus, ScopeRule)

logger = logging.getLogger("resumetrace.jd")

CRITERION_JSON_SCHEMA = {
    "type": "object",
    "required": ["category", "expression"],
    "properties": {
        "category": {"enum": ["responsibility", "required", "preferred", "search_intent", "clarify"]},
        "expression": {"$ref": "#/$defs/node"},
        "min_duration_months": {"type": ["integer", "null"], "minimum": 0},
        "scope_rule": {"enum": ["CANDIDATE_ANYWHERE", "SAME_WORK", "SAME_PROJECT", "WORK_PROJECT_CHAIN"]},
        "weight": {"type": "number", "minimum": 0},
        "input_spans": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
        },
    },
    "$defs": {
        "node": {
            "type": "object",
            "properties": {
                "op": {"enum": ["AND", "OR", None]},
                "children": {"type": "array", "items": {"$ref": "#/$defs/node"}},
                "predicate": {"type": ["string", "null"]},
                "value": {"type": ["string", "null"]},
                "examples": {"type": "array", "items": {"type": "string"}},
            },
        }
    },
}

SYSTEM_PROMPT = """你是招聘条件解析器。把用户输入(自然语言要求或完整 JD)解析为结构化条件数组,只输出 JSON。

严格规则:
1. category 取值:responsibility(岗位职责,描述候选人入职后做什么,不作候选人硬门槛)、
   required(任职要求中明确"必须/要求/具备"的硬条件)、preferred("优先/加分/更佳")、
   search_intent(自然语言未明确必须/优先的搜索意图)、clarify(语义含糊需要确认)。
2. 逻辑关系:同一条件的可选项用 OR 节点(如"Java 或 C/C++");同时满足用 AND。
3. 示例技术:"如/例如/包括但不限于"后面的技术放进 expression.examples,不得变成硬条件;
   条件本体写成能力描述(如"流批处理技术")。
4. 年限:区分"总软件开发年限"与"特定方向/领域年限",分别建条件并填 min_duration_months;
   原文没写年限不得自行补充。
5. input_spans:每个条件给出其在原文中的字符偏移 [start, end] 片段(可多段),必须逐字对应原文。
6. 不得发明原文不存在的条件(如"近三年""必须 Flink");含糊或冲突的条目归入 clarify。
7. 同一 OR 条件引用多处原文时,input_spans 列全部片段。

输出格式:{"criteria": [ ... ]},每个元素字段:
category, expression{op,children,predicate,value,examples}, min_duration_months, scope_rule, weight, input_spans。
原子条件 op 填 "LEAF";组合填 "AND"/"OR"。scope_rule 只能填 CANDIDATE_ANYWHERE/SAME_WORK/SAME_PROJECT/WORK_PROJECT_CHAIN 之一或 null。

示例(仅演示形状):原文"熟悉流批处理技术,如Flink、Spark" 应解析为
{"category":"required","expression":{"op":"LEAF","predicate":"流批处理技术能力","value":null,
"examples":["Flink","Spark"]},"min_duration_months":null,"scope_rule":null,"weight":1,
"input_spans":[[s,e]]}
——Flink/Spark 是示例,放 examples,不得放 children 或单独成硬条件。

示例二:原文"精通Java或C/C++" 应解析为**一个** required 条件:
{"category":"required","expression":{"op":"OR","children":[
 {"op":"LEAF","predicate":"编程语言","value":"Java","examples":[]},
 {"op":"LEAF","predicate":"编程语言","value":"C/C++","examples":[]}],
 "predicate":"编程语言能力","value":null,"examples":[]},"min_duration_months":null,
 "scope_rule":null,"weight":1,"input_spans":[[s,e]]}
——"或"关系必须合成单个 OR 条件,不得拆成两个独立 required。

示例三:自然语言"找做过风控平台架构、熟悉流批处理的人"(未写必须/优先/年限)应解析为:
两条 category="search_intent" 的条件,min_duration_months=null,不得出现 required/preferred,
不得自行补充年限或硬门槛。
"""


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def validate_offsets(spans: list[list[int]], raw_text: str) -> bool:
    for sp in spans:
        if len(sp) != 2 or sp[0] > sp[1] or sp[1] > len(raw_text):
            return False
    return True


def parse_conditions(raw_text: str, llm_call: Callable[[str, str], str],
                     input_type: str = "auto") -> tuple[list[JobCriterion], str, list[str]]:
    """返回 (criteria, query_sha256, warnings)。

    llm_call(system, user) -> 模型原始输出文本。Schema/偏移校验失败的条件
    降级为 clarify+needs_review 并记 warning,不静默丢弃(规格 7.2)。
    """
    query_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    warnings: list[str] = []
    # 模型输出形状偶发漂移:结构非法过半时重试一次(规格要求回归稳定通过)
    payload = None
    last_err = None
    for _attempt in range(3):
        out = llm_call(SYSTEM_PROMPT, raw_text)
        cleaned = re.sub(r"^```(json)?|```$", "", out.strip(), flags=re.M)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            # 兜底:截取首个 { 到最后一个 } 的子串(模型偶发夹带说明文字)
            m = re.search(r"\{.*\}", cleaned, flags=re.S)
            try:
                payload = json.loads(m.group(0)) if m else None
            except (json.JSONDecodeError, AttributeError):
                payload = None
        if payload is None:
            last_err = f"non-json output: {out[:120]!r}"
            continue
        items = payload.get("criteria", [])
        bad = sum(1 for it in items if _build_node(it.get("expression") or {}) is None)
        if items and bad > len(items) // 2:
            payload = None
            last_err = "structure drift"
            continue
        break
    if payload is None:
        raise ValueError(f"条件解析模型输出非法或结构漂移: {last_err}")

    criteria: list[JobCriterion] = []
    logger.info("JD parse attempt %d: %d raw criteria from model", _attempt + 1, len(payload.get("criteria", [])))
    for i, item in enumerate(payload.get("criteria", [])):
        cid = f"c{i + 1:02d}"
        spans = item.get("input_spans", [])
        located = validate_offsets(spans, raw_text)
        # 偏移定位复核:片段文本应能在原文找到(允许空白差异)
        if located:
            for s, e in spans:
                frag = _norm_ws(raw_text[s:e])
                if frag and frag not in _norm_ws(raw_text):
                    located = False
                    break
        review = ReviewStatus.AUTO
        if not located:
            review = ReviewStatus.NEEDS_REVIEW
            warnings.append(f"{cid}: 输入片段偏移无法定位,标记待确认")
            spans = []
        category = item.get("category", "clarify")
        if category not in ("responsibility", "required", "preferred", "search_intent", "clarify"):
            category = "clarify"
            review = ReviewStatus.NEEDS_REVIEW
            warnings.append(f"{cid}: 非法 category,降级 clarify")
        expr = _build_node(item.get("expression") or {})
        if expr is None:
            review = ReviewStatus.NEEDS_REVIEW
            warnings.append(f"{cid}: expression 结构非法,标记待确认")
            expr = ConditionNode(predicate="unparsed", value=raw_text[:80])
        try:
            scope_rule = ScopeRule(item.get("scope_rule") or "CANDIDATE_ANYWHERE")
        except ValueError:
            scope_rule = ScopeRule.CANDIDATE_ANYWHERE
            warnings.append(f"{cid}: 非法 scope_rule,回退 CANDIDATE_ANYWHERE")
        try:
            min_months = item.get("min_duration_months")
            min_months = int(min_months) if min_months is not None else None
            if min_months is not None and min_months < 0:
                min_months = None
        except (TypeError, ValueError):
            min_months = None
            warnings.append(f"{cid}: 非法年限值,忽略")
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        criteria.append(JobCriterion(
            criterion_id=cid,
            query_id="",  # 由调用方填充
            input_spans=[tuple(sp) for sp in spans],
            category=CriterionCategory(category),
            expression=expr,
            min_duration_months=min_months,
            scope_rule=scope_rule,
            weight=weight,
            review_status=review,
        ))
    logger.info("JD parse done: %d criteria, %d warnings", len(criteria), len(warnings))
    return criteria, query_sha, warnings


def _build_node(d) -> Optional[ConditionNode]:
    # 模型输出形状会漂移:dict(LEAF/AND/OR)、纯字符串条件、列表(视为 AND)都接受
    if isinstance(d, str):
        return ConditionNode(predicate=d.strip() or None)
    if isinstance(d, list):
        children = [c for c in (_build_node(x) for x in d) if c is not None]
        return ConditionNode(op="AND" if len(children) > 1 else None,
                             children=children if len(children) > 1 else [],
                             predicate=None if len(children) > 1 else (children[0].predicate if children else None),
                             value=None if len(children) > 1 else (children[0].value if children else None))
    if not isinstance(d, dict):
        return None
    op = d.get("op")
    if op == "LEAF":  # 模型以 LEAF 标记原子节点
        op = None
    children = []
    for c in d.get("children", []) or []:
        node = _build_node(c)
        if node is None:
            return None
        children.append(node)
    if op in ("AND", "OR") and not children:
        return None
    if op not in ("AND", "OR", None):
        return None
    examples = [str(x) for x in d.get("examples", []) or []]
    node = ConditionNode(op=op, children=children, predicate=d.get("predicate"),
                         value=d.get("value"), examples=examples)
    # OR/AND 组自带的 predicate/value 是组描述,并入每个叶子以免语义丢失
    if op in ("AND", "OR") and (d.get("predicate") or d.get("value")):
        group_ctx = " ".join(p for p in (d.get("predicate"), d.get("value")) if p)
        for ch in node.children:
            if not ch.op and ch.predicate:
                ch.predicate = f"{group_ctx} {ch.predicate}"
    return node


def hard_gate_criteria(criteria: list[JobCriterion]) -> list[JobCriterion]:
    """参与硬判定与证据分的条件:required/search_intent 且结构合法(auto/confirmed)。"""
    return [c for c in criteria
            if c.category in (CriterionCategory.REQUIRED, CriterionCategory.SEARCH_INTENT)
            and c.review_status != ReviewStatus.NEEDS_REVIEW]
