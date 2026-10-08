"""P1 退出标准验证:证据字段入库回读、真实坐标、引文对页校验、CV-01/CV-04。

用法: export RAGFLOW_API_KEY=*** && python3 it_p1_evidence.py --dataset-id <b1>
"""
import argparse
import io
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.core.contracts import ResumeVersion, _norm  # noqa: E402
from server.core.evidence import (chunks_to_evidence, es_chunks_for_document,
                                  page_texts)  # noqa: E402

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
KEY = os.environ["RAGFLOW_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
FAKE = lambda pos: all(p[1] == 0 and p[2] == 0 for p in pos)  # 模拟坐标特征


def _quote_ok(s, pages):
    if not s.rects:
        return False
    end = min(max(r.page for r in s.rects), len(pages) - 1)
    blob = "".join(_norm(p) for p in pages[s.page_index:end + 1])
    return _norm(s.quote) in blob


def get(path, **kw):
    return requests.get(BASE + path, headers=H, timeout=120, **kw).json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-id", required=True)
    args = ap.parse_args()
    ds = args.dataset_id
    docs = get(f"/api/v1/datasets/{ds}/documents?page=1&page_size=50")["data"]["docs"]
    results = []

    def check(cid, ok, detail=""):
        results.append((cid, bool(ok), detail))

    # IT-ES-01/02: 字段入库 + 按 document_id+scope_id 回读
    field_docs = 0
    scope_index = defaultdict(set)
    all_chunks = {}
    for d in docs:
        chs = es_chunks_for_document(TENANT, d["id"])
        all_chunks[d["id"]] = chs
        has = [c for c in chs if c.get("resume_scope_id_kwd")]
        if has:
            field_docs += 1
        for c in has:
            scope_index[(d["id"], c["resume_scope_id_kwd"])].add(c["id"])
    check("IT-ES-01 字段入库", field_docs == len(docs), f"{field_docs}/{len(docs)} 文档带 scope 字段")
    check("IT-ES-02 按 doc+scope 回读", len(scope_index) >= field_docs * 2,
          f"doc+scope 组合 {len(scope_index)}")

    # ES mapping 类型(容器内查)
    import subprocess
    out = subprocess.run(
        ["docker", "exec", "deploy-es01-1", "curl", "-s", "-u",
         "elastic:infini_rag_flow", "localhost:9200/ragflow_*"],
        capture_output=True, text=True).stdout
    types = {}
    try:
        mapping_blob = json.loads(out)
        for idx, body in mapping_blob.items():
            props = body["mappings"]["properties"]
            for f in ("resume_scope_id_kwd", "resume_scope_kind_kwd",
                      "resume_source_ref_id_kwd", "resume_span_status_kwd"):
                if f in props:
                    types[f] = props[f]["type"]
            break
    except Exception as e:
        types = {"error": str(e)[:60]}
    check("IT-ES-03 mapping 类型", all(v == "keyword" for v in types.values()) and len(types) >= 4,
          json.dumps(types, ensure_ascii=False))

    # 逐文档:真实坐标 + 引文对页 + scope 种类
    by_name = {d["name"]: d for d in docs}
    located_tot = span_tot = 0
    for name, d in sorted(by_name.items()):
        chs = all_chunks[d["id"]]
        located = [c for c in chs if c.get("resume_span_status_kwd") == "located"]
        unloc = [c for c in chs if c.get("resume_span_status_kwd") == "unlocated"]
        fake_pos = [c for c in located if FAKE(c.get("position_int") or [])]
        span_tot += len(located) + len(unloc)
        located_tot += len(located)
        pdf = requests.get(BASE + f"/api/v1/datasets/{ds}/documents/{d['id']}",
                           headers=H, timeout=120).content
        pages = page_texts(pdf)
        ver = ResumeVersion(candidate_id=name[:4], dataset_id=ds, document_id=d["id"],
                            source_sha256="", filename=name, ingested_at="",
                            parser_version="v0.27.2+0002", status="DONE")
        spans, scopes = chunks_to_evidence(chs, ver, pages)
        bad_quote = [s for s in spans.values()
                     if s.status == "located" and s.extraction_mode == "metadata"
                     and not _quote_ok(s, pages)]
        kinds = Counter(s.kind.value for s in scopes.values())
        tag = name[:4]
        check(f"{tag} 无模拟坐标冒充", not fake_pos, f"located={len(located)} fake={len(fake_pos)}")
        check(f"{tag} 引文对页匹配", not bad_quote, f"bad={len(bad_quote)}")
        if tag == "CV02":
            check("CV-01 scope 分离", kinds.get("project", 0) >= 2 and kinds.get("work", 0) >= 2,
                  json.dumps(dict(kinds)))
        if tag == "CV04":
            check("CV-04a 无日期仍定位", len(located) > 0, f"located={len(located)}")
        if tag == "CV10":
            modes = {s.extraction_mode for s in spans.values()}
            check("CV-04b 扫描件标 OCR", modes == {"OCR"} and len(located) > 0,
                  f"modes={modes} located={len(located)}")
        if tag == "CV09":
            pages_used = {s.page_index for s in spans.values()}
            check("CV09 双栏真实坐标", len(located) > 0 and not fake_pos, f"pages={pages_used}")

    check("定位覆盖率(全库)", located_tot / max(1, span_tot) >= 0.9,
          f"{located_tot}/{span_tot} = {located_tot / max(1, span_tot):.2%}")

    width = max(len(c) for c, _, _ in results)
    failed = 0
    for cid, ok, detail in results:
        print(f"{cid:<{width}}  {'PASS' if ok else 'FAIL'}  {detail}")
        failed += 0 if ok else 1
    print("\nP1 验收:", "全部通过" if failed == 0 else f"{failed} 项失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
