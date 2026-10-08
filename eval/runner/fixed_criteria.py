"""中信期货 J10034 人工审定固定条件集(评测用,规格第 2 节)。

评测/对照跑使用本固定集,不随模型解析漂移;实时解析仅用于演示流与 JD 回归。
input_spans 在加载时按原文子串定位,定位失败抛错(不允许静默)。
"""
import json
from pathlib import Path

from server.core.contracts import (ConditionNode, CriterionCategory, JobCriterion,
                                   ReviewStatus, ScopeRule)

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "jd" / "citic_j10034.json"

_SPEC = [
    ("c_lang", CriterionCategory.REQUIRED, None,
     ConditionNode(op="OR", children=[
         ConditionNode(predicate="编程语言", value="Java"),
         ConditionNode(predicate="编程语言", value="C/C++"),
     ], predicate="编程语言能力"),
     ["精通Java或C/C++"], None),
    ("c_dev5", CriterionCategory.REQUIRED, 60,
     ConditionNode(predicate="金融行业软件开发经验"),
     ["5年以上金融行业软件开发经验"], None),
    ("c_dir3", CriterionCategory.REQUIRED, 36,
     ConditionNode(predicate="风控平台、交易平台或相关方向的项目开发与架构经验"),
     ["3年以上风控平台、交易平台或相关方向的项目开发与架构经验"], None),
    ("c_stream", CriterionCategory.REQUIRED, None,
     ConditionNode(predicate="流批处理技术能力", examples=["Flink", "Spark"]),
     ["熟悉流批处理技术,如Flink、Spark"], None),
    ("p_risk_stream", CriterionCategory.PREFERRED, None,
     ConditionNode(predicate="实时风控或交易类流批项目经验"),
     ["有实时风控或交易类流批项目经验者优先"], None),
    ("p_lead", CriterionCategory.RESPONSIBILITY, None,
     ConditionNode(predicate="主导技术方案设计与评审"),
     ["主导技术方案设计与评审"], None),
]


def load_fixed_criteria() -> list[JobCriterion]:
    raw = json.loads(FIX.read_text(encoding="utf-8"))["raw_text"]
    out = []
    for cid, cat, months, expr, frags, rule in _SPEC:
        spans = []
        for f in frags:
            i = raw.find(f)
            if i < 0:
                raise ValueError(f"固定条件片段在原文中不存在: {f}")
            spans.append((i, i + len(f)))
        out.append(JobCriterion(
            criterion_id=cid, query_id="fixed-J10034", input_spans=spans,
            category=cat, expression=expr, min_duration_months=months,
            scope_rule=rule or ScopeRule.CANDIDATE_ANYWHERE, weight=1.0,
            review_status=ReviewStatus.CONFIRMED))
    return out
