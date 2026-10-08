"""JD 条件解析回归(规格 12 节 JD-01~04 + NL-01)。

用法:
  export RAGFLOW_API_KEY=***
  python3 test_jd_regression.py

断言基于 fixtures/jd/citic_j10034.json 的人工审定条件结构;模型输出必须复现该结构,
不允许自行改写(规格 7.2)。
"""
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.jd.parser import parse_conditions  # noqa: E402
from server.core.contracts import CriterionCategory  # noqa: E402

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
KEY = os.environ["RAGFLOW_API_KEY"]
PROVIDER = os.environ.get("RAGFLOW_PROVIDER", "OpenAI-API-Compatible")
INSTANCE = os.environ.get("RAGFLOW_INSTANCE", "dashscope")
CHAT_MODEL = os.environ.get("RAGFLOW_CHAT_MODEL", "deepseek-v4.1-flash")

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "jd" / "citic_j10034.json"


def llm_call(system: str, user: str) -> str:
    r = requests.post(
        f"{BASE}/api/v1/providers/{PROVIDER}/instances/{INSTANCE}/models/{CHAT_MODEL}",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"message": f"{system}\n\n---\n用户输入原文:\n{user}", "thinking": False},
        timeout=300,
    )
    body = r.json()
    if body.get("code") != 0:
        raise SystemExit(f"chat 失败: {body}")
    ans = body["data"]["answer"]
    if isinstance(ans, str) and ans.lstrip().startswith("**ERROR**"):
        raise RuntimeError(f"模型调用失败: {ans[:160]}")
    return ans


def find_atomic(criteria, keyword):
    out = []
    for c in criteria:
        stack = [c.expression]
        while stack:
            n = stack.pop()
            if n.predicate or n.value:
                blob = f"{n.predicate or ''} {n.value or ''}"
                if keyword in blob:
                    out.append(c)
            stack.extend(n.children)
    return out


def main():
    fix = json.loads(FIX.read_text(encoding="utf-8"))
    raw = fix["raw_text"]
    criteria, sha, warnings = parse_conditions(raw, llm_call)
    results = []

    def check(cid, ok, detail=""):
        results.append((cid, ok, detail))

    # JD-01: Java 与 C/C++ 解析为一个 OR 组合,两处原文片段均可定位
    ors = [c for c in criteria if c.expression.op == "OR"]
    lang_or = None
    for c in ors:
        leaves = " ".join((n.predicate or "") + (n.value or "") for n in c.expression.children)
        if "Java" in leaves and ("C/C++" in leaves or "C++" in leaves):
            lang_or = c
    check("JD-01", lang_or is not None and len(lang_or.input_spans) >= 1
          and lang_or.review_status.value == "auto",
          f"OR 条件={lang_or is not None}, spans={len(lang_or.input_spans) if lang_or else 0}")

    # JD-02: Flink/Spark 为示例,不得成为硬条件
    stream = [c for c in criteria if any("流批" in ((n.predicate or "") + (n.value or ""))
                                         for n in [c.expression] + c.expression.children)]
    examples = set()
    for c in stream:
        stack = [c.expression]
        while stack:
            n = stack.pop()
            examples.update(n.examples)
            stack.extend(n.children)
    flink_hard = [c for c in criteria
                  if c.category == CriterionCategory.REQUIRED
                  and any(("Flink" in ((n.predicate or "") + (n.value or "")))
                          and not n.op for n in [c.expression] + c.expression.children)
                  and not c.expression.examples]
    check("JD-02", bool(stream) and {"Flink", "Spark"} <= examples and not flink_hard,
          f"examples={sorted(examples)}, flink_hard={len(flink_hard)}")

    # JD-03: 实时风控/交易流批项目经验只进 preferred
    pref = [c for c in criteria if c.category == CriterionCategory.PREFERRED]
    req_risk_stream = [c for c in criteria
                       if c.category == CriterionCategory.REQUIRED
                       and any("优先" in raw[s:e] for s, e in c.input_spans)]
    check("JD-03", bool(pref) and not req_risk_stream,
          f"preferred={len(pref)}, 误升硬条件={len(req_risk_stream)}")

    # JD-04: 主导方案设计归职责,不产生候选人既往主导硬条件
    resp = [c for c in criteria if c.category == CriterionCategory.RESPONSIBILITY]
    lead_hard = [c for c in criteria
                 if c.category == CriterionCategory.REQUIRED
                 and find_atomic([c], "主导")]
    check("JD-04", bool(resp) and not lead_hard,
          f"responsibility={len(resp)}, 主导硬条件={len(lead_hard)}")

    # 禁止发明的条件
    blob = json.dumps([c.model_dump() for c in criteria], ensure_ascii=False)
    forbidden = [f for f in fix["forbidden_criteria"] if f.split("(")[0] in blob]
    check("JD-05-no-invent", not forbidden, f"出现禁止条件={forbidden}")

    # NL-01: 自然语言直搜,条件归搜索意图,不自行补年限/硬门槛
    nl, _, nl_warn = parse_conditions("找做过风控平台架构、熟悉流批处理的人", llm_call)
    bad = [c for c in nl if c.category in (CriterionCategory.REQUIRED, CriterionCategory.PREFERRED)
           or c.min_duration_months]
    check("NL-01", bool(nl) and not bad, f"条件数={len(nl)}, 越界={len(bad)}, warnings={nl_warn}")

    width = max(len(c) for c, _, _ in results)
    failed = 0
    for cid, ok, detail in results:
        print(f"{cid:<{width}}  {'PASS' if ok else 'FAIL'}  {detail}")
        failed += 0 if ok else 1
    print(f"\nquery_sha256={sha}\nwarnings={warnings}")
    print("回归结果:", "全部通过" if failed == 0 else f"{failed} 项失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
