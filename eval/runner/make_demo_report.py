"""生成可演示的筛选报告(独立 HTML,离线可开;在线高亮走 /ui/)。

用法: python3 make_demo_report.py [--arm-dir e2e_b1]
产物: eval/results/demo/筛选报告.html
"""
import argparse
import html
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.core.evidence import es_chunks_for_document  # noqa: E402

RES = Path(__file__).resolve().parents[1] / "results"
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")

STATE_CLS = {"SUPPORTED": "ok", "PARTIAL": "warn", "NOT_EVIDENCED": "no",
             "NEEDS_REVIEW": "warn", "CONFLICTING": "bad"}
LABEL = {"c_lang": "语言", "c_dev5": "金融年限", "c_dir3": "方向年限",
         "c_stream": "流批处理", "p_risk_stream": "风控项目", "p_lead": "主导设计"}


STATE_CN = {"SUPPORTED": "满足", "PARTIAL": "部分满足", "NOT_EVIDENCED": "简历没写",
            "NEEDS_REVIEW": "待确认", "CONFLICTING": "原文冲突"}


def verdict_of(rep):
    items = rep["items"]
    req = [i for i in items if i["category"] in ("required", "search_intent")]
    sup = sum(1 for i in req if i["state"] == "SUPPORTED")
    pend = sum(1 for i in items if i["state"] == "NEEDS_REVIEW")
    if any(i["state"] == "CONFLICTING" for i in items):
        return "bad", "原文有冲突,先人工核对再决定"
    if req and sup == len(req) and not pend:
        return "ok", f"建议进入复核:硬性要求 {sup}/{len(req)} 条都有原文证据"
    if sup > 0:
        return "mid", f"可进入复核,先确认 {pend + len(req) - sup} 处(硬性要求 {sup}/{len(req)} 条有原文证据)"
    return "bad", "建议暂缓:硬性要求缺少原文证据"

CSS = """
body{font-family:-apple-system,'PingFang SC',sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1a3c6e;color:#fff;padding:16px 24px}
header h1{margin:0;font-size:20px} header .muted{color:#bcd}
main{max-width:1000px;margin:24px auto;padding:0 16px}
table{width:100%;border-collapse:collapse;background:#fff}
th,td{border:1px solid #e3e6ea;padding:8px 10px;font-size:13px;text-align:left}
th{background:#eef2f7}
.cand{background:#fff;border:1px solid #e3e6ea;border-radius:10px;margin:18px 0;padding:16px 18px}
.cand h2{margin:0 0 4px;font-size:17px}
.pill{display:inline-block;border-radius:10px;padding:1px 10px;font-size:12px;margin-left:6px}
.pill.ok{background:#e5f6ee;color:#0a7a5a}.pill.warn{background:#fff4e5;color:#a05a00}
.pill.no{background:#eee;color:#666}.pill.bad{background:#fdecec;color:#a00}
.item{border-left:3px solid #ddd;margin:10px 0;padding:8px 12px;background:#fafbfc}
.item.ok{border-color:#0a7a5a}.item.warn{border-color:#e6a23c}
.item.no{border-color:#bbb}.item.bad{border-color:#a00}
cite{display:block;background:#f4f8f4;border:1px dashed #9cb;border-radius:6px;padding:6px 9px;margin:5px 0;font-style:normal;font-size:13px}
.q{background:#fff8ec;border-radius:6px;padding:6px 10px;font-size:13px;margin:6px 0}
pre{background:#f7f7f9;border:1px solid #e3e6ea;border-radius:6px;padding:8px;font-size:12px;overflow:auto}
.muted{color:#777;font-size:12px}
.verdict{font-size:15px;font-weight:700;margin:4px 0}
.verdict.ok{color:#0a7a5a}.verdict.mid{color:#a05a00}.verdict.bad{color:#666}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm-dir", default="e2e_b1")
    args = ap.parse_args()
    arm = RES / args.arm_dir
    rows = [json.loads(l) for l in (arm / "summary.jsonl").read_text(encoding="utf-8").splitlines()]
    rows.sort(key=lambda r: -r["score"])
    audit = json.loads((arm / "audit.json").read_text(encoding="utf-8"))
    man_path = RES / "demo" / "pages_manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    crit = {c["criterion_id"]: c for c in audit["criteria"]}

    parts = [f"<html><head><meta charset='utf-8'><style>{CSS}</style>"
             "<title>ResumeTrace 筛选报告</title></head><body>",
             "<header><h1>候选人筛选报告 · 应用架构师(风控平台方向) J10034</h1>"
             "<div class='muted'>ResumeTrace · RAGFlow v0.27.2 二开 · 证据可溯源到简历原文页/坐标 · "
             "合成简历测试数据,非真实候选人</div></header><main>",
             "<h2>排序(当前证据匹配分)</h2><table><tr><th>#</th><th>候选人</th><th>证据匹配分</th>"
             "<th>覆盖率</th><th>逐项状态</th></tr>"]
    for i, r in enumerate(rows, 1):
        states = " ".join(f"<span class='pill {STATE_CLS.get(v, 'no')}'>{LABEL.get(k, k)} {STATE_CN.get(v, v)}</span>"
                          for k, v in r["states"].items())
        parts.append(f"<tr><td>{i}</td><td><a href='#{r['candidate']}'>{r['filename'][:18]}</a></td>"
                     f"<td><b>{r['score']}</b></td><td>{r['coverage']}%</td><td>{states}</td></tr>")
    parts.append("</table>")

    for r in rows:
        rep = json.loads((arm / f"report_{r['candidate']}.json").read_text(encoding="utf-8"))
        refmap = {}
        try:
            for ck in es_chunks_for_document(TENANT, rep["candidate"]["document_id"]):
                for ref in ck.get("resume_source_ref_id_kwd") or []:
                    refmap.setdefault(ref, (ck.get("content_with_weight") or "")[:220])
        except Exception:
            pass
        vcls, vtext = verdict_of(rep)
        parts.append(f"<div class='cand' id='{r['candidate']}'><h2>{rep['candidate']['filename']}</h2>"
                     f"<div class='verdict {vcls}'>{vtext}</div>"
                     f"<div class='muted'>证据匹配分 {rep['score']['score']}/100 · 简历 sha256 {rep['candidate']['source_sha256'][:12]}…</div>")
        for it in rep["items"]:
            if it["category"] == "responsibility":
                continue
            parts.append(f"<div class='item {STATE_CLS.get(it['state'], 'no')}'>"
                         f"<b>{STATE_CN.get(it['state'], it['state'])}</b> "
                         f"<span class='muted'>{html.escape(' / '.join(it['input_fragments']) or '(无定位片段)')}</span>")
            for c in it["citations"]:
                exc = refmap.get(c.get("span_id"), "")
                png = manifest.get(r["candidate"], {}).get(f"{it['criterion_id']}|{c.get('span_id')}")
                parts.append(f"<cite>📄 p{c['page']} [{c['label']}] {html.escape(c['quote'])}"
                             + (f"<div class='muted'>证据正文:{html.escape(exc)}</div>" if exc else "")
                             + (f"<img src='{png}' style='max-width:100%;border:1px solid #ccd;border-radius:6px;margin-top:6px'>" if png else "")
                             + "</cite>")
            if it["review_question"]:
                parts.append(f"<div class='q'>❓ 待确认:{html.escape(it['review_question'])}</div>")
            parts.append("</div>")
        if rep["review_questions"]:
            parts.append("<h3>建议进一步确认</h3>" +
                         "".join(f"<div class='q'>({q['criterion_id']}) {html.escape(q['question'])}</div>"
                                 for q in rep["review_questions"]))
        parts.append("<details><summary>评分细目</summary><pre>"
                     + html.escape(json.dumps(rep["score"]["lines"], ensure_ascii=False, indent=1))
                     + "</pre></details></div>")

    parts.append(f"<p class='muted'>引用审计:无引证 SUPPORTED = {len(audit.get('unaudited_supported', []))};"
                 "所有'原文支持'结论均可点击页码在 /ui/ 查看 PDF 高亮(矩形坐标来自解析期行映射,"
                 "引文经归一化对页校验)。本报告由结构化 CriterionResult 确定性渲染,未用生成模型润色。</p>")
    parts.append("</main></body></html>")
    outdir = RES / "demo"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "筛选报告.html").write_text("".join(parts), encoding="utf-8")
    print("demo report:", outdir / "筛选报告.html")


if __name__ == "__main__":
    main()
