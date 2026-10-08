"""端到端:JD → 条件 → 召回 → 同经历校验判断 → 评分 → 报告 + 引用审计(P3/P4)。

用法:
  export RAGFLOW_API_KEY=***
  python3 e2e_search_report.py --dataset-id <b1> [--top-n 20] [--scope-constraints on|off]

--scope-constraints off 复现 B0 判断风格(不做同经历校验),供 P5 对照。
产物:eval/results/e2e_<prefix>/report_<CVxx>.json|.md + summary.jsonl
"""
import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.core.contracts import (CriterionCategory, CriterionState, ResumeVersion,
                                   ScopeRule, validate_invariants)
from server.core.evidence import (chunks_to_evidence, es_chunks_for_document,
                                  page_texts)
from server.jd.parser import hard_gate_criteria, parse_conditions
from server.report.render import render_report, score_report, to_markdown
from server.search.orchestrator import judge_criterion
from server.search.pipeline import criterion_queries

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
KEY = os.environ["RAGFLOW_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
PROVIDER = os.environ.get("RAGFLOW_PROVIDER", "OpenAI-API-Compatible")
INSTANCE = os.environ.get("RAGFLOW_INSTANCE", "dashscope")
CHAT_MODEL = os.environ.get("RAGFLOW_CHAT_MODEL", "deepseek-v4.1-flash")
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "jd" / "citic_j10034.json"
GT = Path.home() / "Desktop/简历库/ground_truth"


def llm_call(system: str, user: str) -> str:
    msg = f"{system}\n\n---\n用户输入原文:\n{user}" if system else user
    r = requests.post(f"{BASE}/api/v1/providers/{PROVIDER}/instances/{INSTANCE}/models/{CHAT_MODEL}",
                      headers={**H, "Content-Type": "application/json"},
                      json={"message": msg, "thinking": False}, timeout=300)
    body = r.json()
    if body.get("code") != 0:
        raise SystemExit(f"chat 失败: {body}")
    ans = body["data"]["answer"]
    if isinstance(ans, str) and ans.lstrip().startswith("**ERROR**"):
        raise RuntimeError(f"模型调用失败: {ans[:160]}")
    return ans


def retrieval(question: str, dataset_id: str, top_k: int = 20):
    r = requests.post(f"{BASE}/api/v1/retrieval", headers={**H, "Content-Type": "application/json"},
                      json=dict(question=question, dataset_ids=[dataset_id], top_k=top_k,
                                similarity_threshold=0.1, keyword=True), timeout=120)
    return r.json().get("data", {}).get("chunks", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--scope-constraints", choices=["on", "off"], default="on")
    ap.add_argument("--prefix", default=None)
    ap.add_argument("--fixed", action="store_true",
                    help="评测用人工审定固定条件集(规格第 2 节),不随模型解析漂移")
    args = ap.parse_args()
    prefix = args.prefix or f"e2e_{'b1' if args.scope_constraints == 'on' else 'b0style'}"
    outdir = Path(__file__).resolve().parents[1] / "results" / prefix
    outdir.mkdir(parents=True, exist_ok=True)

    fix = json.loads(FIX.read_text(encoding="utf-8"))
    raw = fix["raw_text"]
    if args.fixed:
        from fixed_criteria import load_fixed_criteria
        criteria = load_fixed_criteria()
        qsha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        warns = []
    else:
        criteria, qsha, warns = parse_conditions(raw, llm_call)
    for i, c in enumerate(criteria):
        c.query_id = f"q-{qsha[:8]}"
    print(f"条件 {len(criteria)} 条,warnings={warns}")

    # 阶段一:逐条件召回,候选池=并集,记录每候选人最初由哪些 chunk 召回
    pool: dict[str, list[str]] = {}
    for c in hard_gate_criteria(criteria) or criteria:
        for q in criterion_queries(c):
            for hit in retrieval(q, args.dataset_id):
                pool.setdefault(hit["document_id"], []).append(hit.get("id") or hit.get("chunk_id"))

    docs = requests.get(f"{BASE}/api/v1/datasets/{args.dataset_id}/documents?page=1&page_size=50",
                        headers=H, timeout=60).json()["data"]["docs"]
    doc_by_id = {d["id"]: d for d in docs}
    candidates = sorted(pool, key=lambda d: -len(pool[d]))[:args.top_n]
    print(f"候选池 {len(pool)} → 取前 {len(candidates)}")

    as_of = datetime.date.today()
    summary = []
    all_facts, all_results, all_spans = [], [], {}
    for doc_id in candidates:
        d = doc_by_id[doc_id]
        tag = d["name"][:4]
        chs = es_chunks_for_document(TENANT, doc_id)
        pdf = requests.get(f"{BASE}/api/v1/datasets/{args.dataset_id}/documents/{doc_id}",
                           headers=H, timeout=120).content
        sha = hashlib.sha256(pdf).hexdigest()
        pages = page_texts(pdf)
        ver = ResumeVersion(candidate_id=tag, dataset_id=args.dataset_id, document_id=doc_id,
                            source_sha256=sha, filename=d["name"],
                            ingested_at=d.get("create_date", ""), parser_version="v0.27.2+0002",
                            status="DONE")
        spans, scopes = chunks_to_evidence(chs, ver, pages)
        all_spans.update(spans)
        scopes_by_id = {s.scope_id: s for s in scopes.values()}
        recalled = {cid for cid in pool[doc_id]}
        results, facts = [], []
        for c in criteria:
            cc = c
            if args.scope_constraints == "off":
                cc = c.model_copy(update={"scope_rule": ScopeRule.CANDIDATE_ANYWHERE})
            if args.scope_constraints == "on":
                consider = [ck for ck in chs if ck.get("resume_scope_id_kwd")]
            else:
                consider = chs
            r, fs = judge_criterion(cc, tag, consider, scopes_by_id, llm_call, as_of,
                                    model_version=CHAT_MODEL,
                                    scope_constraints=(args.scope_constraints == "on"),
                                    jd_text=raw)
            results.append(r)
            facts.extend(fs)
        all_results.extend(results)
        all_facts.extend(facts)
        validate_invariants([ver], spans, facts, results)
        score = score_report(criteria, results)
        report = render_report(raw, qsha, criteria, results, ver, spans, score)
        (outdir / f"report_{tag}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (outdir / f"report_{tag}.md").write_text(to_markdown(report), encoding="utf-8")
        summary.append(dict(candidate=tag, filename=d["name"], score=score["score"],
                            coverage=score["coverage_pct"],
                            states={r.criterion_id: r.state.value for r in results}))
        print(f"{tag} score={score['score']} coverage={score['coverage_pct']}%")

    # 引用审计:每个 SUPPORTED 结论必须有 located 引证(规格 RPT-01 类)
    unaudited = [r.criterion_id + "/" + r.candidate_id for r in all_results
                 if r.state == CriterionState.SUPPORTED
                 and not any(all_spans.get(s) and all_spans[s].status == "located"
                             for s in r.source_span_ids)]
    (outdir / "summary.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in summary), encoding="utf-8")
    audit = dict(unaudited_supported=unaudited,
                 criteria=[c.model_dump() for c in criteria],
                 warnings=warns, scope_constraints=args.scope_constraints)
    (outdir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print("\n引用审计: 无引证 SUPPORTED =", len(unaudited), unaudited[:5])
    print("产物目录:", outdir)
    sys.exit(1 if unaudited else 0)


if __name__ == "__main__":
    main()
