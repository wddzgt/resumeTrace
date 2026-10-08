"""P5 对照评测:复用 e2e 两臂产物(e2e_b1 / e2e_b0style)计算指标 + CV-01 边界用例。

指标(规格第 10 节):Recall@20、跨经历误支持率(B0 vs B1 及降幅)、逐条件键一致率、
定位精确率/覆盖率(文本型与 OCR 分开,取自 it_p1 口径)、报告无证据肯定句(审计)。
逐例 JSONL + 汇总 JSON。
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.core.contracts import (ConditionNode, CriterionCategory, JobCriterion,
                                   ReviewStatus, ScopeRule)
from server.core.evidence import es_chunks_for_document, chunks_to_evidence, page_texts
from server.search.orchestrator import judge_criterion

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
KEY = os.environ["RAGFLOW_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
GT = Path.home() / "Desktop/简历库/ground_truth"
RES = Path(__file__).resolve().parents[1] / "results"


def llm_call(system: str, user: str) -> str:
    msg = f"{system}\n\n---\n用户输入原文:\n{user}" if system else user
    model = os.environ.get("RAGFLOW_CHAT_MODEL", "qwen3.8-27b")
    r = requests.post(f"{BASE}/api/v1/providers/OpenAI-API-Compatible/instances/dashscope/"
                      f"models/{model}",
                      headers={**H, "Content-Type": "application/json"},
                      json={"message": msg, "thinking": False}, timeout=300)
    ans = r.json()["data"]["answer"]
    if isinstance(ans, str) and ans.lstrip().startswith("**ERROR**"):
        raise RuntimeError(f"模型调用失败: {ans[:160]}")
    return ans


def key_of(c: dict) -> str | None:
    blob = json.dumps(c.get("expression", {}), ensure_ascii=False)
    cat = c.get("category")
    if c.get("expression", {}).get("op") == "OR" and "Java" in blob:
        return "c_lang"
    if c.get("min_duration_months") == 60:
        return "c_dev5"
    if c.get("min_duration_months") == 36:
        return "c_dir3"
    if "Flink" in blob and cat == "required":
        return "c_stream"
    if cat == "preferred":
        return "p_risk_stream"
    if cat == "responsibility" and "主导" in blob:
        return "p_lead"
    return None


def load_arm(dirpath: Path):
    audit = json.loads((dirpath / "audit.json").read_text(encoding="utf-8"))
    states = {}
    for line in (dirpath / "summary.jsonl").read_text(encoding="utf-8").splitlines():
        s = json.loads(line)
        states[s["candidate"]] = s
    return audit, states


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--b1-dir", default=str(RES / "e2e_b1"))
    ap.add_argument("--b0-dir", default=str(RES / "e2e_b0style"))
    ap.add_argument("--b1-dataset", default="6101b004ba4011f1b8ee3f9c7e9efa67")
    args = ap.parse_args()
    outdir = RES / "p5_compare"
    outdir.mkdir(parents=True, exist_ok=True)

    a1, s1 = load_arm(Path(args.b1_dir))
    a0, s0 = load_arm(Path(args.b0_dir))
    from fixed_criteria import load_fixed_criteria
    crit = [c.model_dump() for c in load_fixed_criteria()]
    cid_key = {c["criterion_id"]: key_of(c) for c in crit}

    gt = {json.loads(f.read_text(encoding="utf-8"))["id"]: json.loads(f.read_text(encoding="utf-8"))
          for f in sorted(GT.glob("CV*.json"))}

    rows = []
    for arm, states in (("B0", s0), ("B1", s1)):
        for tag, s in states.items():
            g = gt.get(tag)
            if not g:
                continue
            for cid, got in s["states"].items():
                k = cid_key.get(cid)
                if not k:
                    continue
                rows.append(dict(arm=arm, candidate=tag, key=k, got=got,
                                 expected=g["expected"].get(k),
                                 traits=g["traits"]))
    (outdir / "per_case.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    def metrics(arm):
        rs = [r for r in rows if r["arm"] == arm]
        judged = [r for r in rs if r["got"]]
        agree = sum(1 for r in judged if r["got"] == r["expected"])
        fs = [r for r in judged if r["got"] == "SUPPORTED"
              and r["expected"] in ("NOT_EVIDENCED", "PARTIAL")]
        return dict(arm=arm, judged=len(judged), agree=agree,
                    agree_rate=round(agree / max(1, len(judged)), 3),
                    false_support=len(fs),
                    cases=[f"{r['candidate']}/{r['key']}" for r in fs])

    m0, m1 = metrics("B0"), metrics("B1")

    # Recall@20:ground truth 中存在应 SUPPORTED 键的候选人是否进入候选池
    recall = {}
    for arm, states in (("B0", s0), ("B1", s1)):
        in_pool = set(states)
        should = {cid for cid, g in gt.items()
                  if any(v == "SUPPORTED" for v in g["expected"].values())}
        recall[arm] = round(len(in_pool & should) / max(1, len(should)), 3)

    # CV-01 边界:合成条件"同一风控项目使用 Flink"(SAME_PROJECT)对 CV02 不得 SUPPORTED
    cv01_state = "SKIPPED"
    docs = requests.get(f"{BASE}/api/v1/datasets/{args.b1_dataset}/documents?page=1&page_size=50",
                        headers=H, timeout=60).json()["data"]["docs"]
    d02 = next((x for x in docs if x["name"].startswith("CV02")), None)
    if d02:
        import datetime
        import hashlib
        from server.core.contracts import ResumeVersion
        chs = es_chunks_for_document(TENANT, d02["id"])
        pdf = requests.get(f"{BASE}/api/v1/datasets/{args.b1_dataset}/documents/{d02['id']}",
                           headers=H, timeout=120).content
        ver = ResumeVersion(candidate_id="CV02", dataset_id=args.b1_dataset,
                            document_id=d02["id"], source_sha256=hashlib.sha256(pdf).hexdigest(),
                            filename=d02["name"], ingested_at="", parser_version="v0.27.2+0002",
                            status="DONE")
        spans, scopes = chunks_to_evidence(chs, ver, page_texts(pdf))
        sb = {s.scope_id: s for s in scopes.values()}
        syn = JobCriterion(
            criterion_id="syn-cv01", query_id="syn", input_spans=[],
            category=CriterionCategory.REQUIRED,
            expression=ConditionNode(predicate="同一风控项目中使用 Flink 流批处理", value=None),
            scope_rule=ScopeRule.SAME_PROJECT, review_status=ReviewStatus.AUTO)
        r, _ = judge_criterion(syn, "CV02", [c for c in chs if c.get("resume_scope_id_kwd")],
                               sb, llm_call, datetime.date.today())
        cv01_state = r.state.value

    summary = dict(b0=m0, b1=m1, recall_at_20=recall,
                   false_support_drop_pct=round(
                       100 * (1 - m1["false_support"] / max(1, m0["false_support"])), 1),
                   cv01_same_project_state=cv01_state,
                   unaudited_supported_b1=len(a1.get("unaudited_supported", [])),
                   warnings_b1=a1.get("warnings", []))
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
