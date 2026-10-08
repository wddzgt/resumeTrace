# -*- coding: utf-8 -*-
"""P0 基线验证:未修改 RAGFlow v0.27.2 的导入/解析/检索表现(B0 对照组)。

用法:
  export RAGFLOW_API_KEY=***        # 测试启动时向用户询问
  python3 p0_baseline.py [--resume-dir DIR] [--out DIR]

流程:建 dataset → 上传桌面简历库全部 PDF → 触发解析并轮询 → 用锚点 JD 派生的
查询跑官方 retrieval → 结果落 eval/results/b0_*.jsonl,供二开后同输入对照。
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
API = BASE + "/api/v1"
KEY = os.environ.get("RAGFLOW_API_KEY", "")
if not KEY:
    sys.exit("缺少 RAGFLOW_API_KEY 环境变量(测试前向用户询问后 export)")

HEAD = {"Authorization": f"Bearer {KEY}"}

# 锚点 JD:中信期货 应用架构师(风控平台方向) J10034,人工审定条件派生的检索表达
QUERIES = [
    ("q_full_jd", "应用架构师 风控平台方向:5年以上金融行业软件开发,3年以上风控或交易平台经验,"
                  "精通Java或C/C++,熟悉流批处理技术如Flink、Spark,有实时风控或交易类流批项目经验者优先"),
    ("q_stream", "Flink 实时风控平台 流批处理 架构设计"),
    ("q_lang_or", "Java 或 C/C++ 交易系统 风控 开发"),
    ("q_lead", "主导风控平台技术方案设计"),
]


def call(method, path, **kw):
    r = requests.request(method, API + path, headers=HEAD, timeout=120, **kw)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if body.get("code") not in (0, None):
        raise SystemExit(f"RAGFlow API 错误 {method} {path}: {body}")
    return body.get("data", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume-dir", default=str(Path.home() / "Desktop/简历库/resumes"))
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "results"))
    ap.add_argument("--dataset", default=os.environ.get("RAGFLOW_DATASET", "resumetrace-b0"))
    ap.add_argument("--prefix", default=os.environ.get("OUT_PREFIX", "b0"))
    ap.add_argument("--search-only", action="store_true",
                    help="跳过建库/上传/解析,仅对已有 dataset 重跑检索")
    ap.add_argument("--dataset-id", default=os.environ.get("RAGFLOW_DATASET_ID", ""))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = []

    def step(name, **fields):
        rec = dict(step=name, ts=time.strftime("%Y-%m-%d %H:%M:%S"), **fields)
        log.append(rec)
        print(json.dumps(rec, ensure_ascii=False))

    if args.search_only:
        if not args.dataset_id:
            sys.exit("--search-only 需要 --dataset-id")
        ds_id = args.dataset_id
        step("search_only", dataset_id=ds_id)
    else:
        t0 = time.time()
        ds_payload = dict(name=args.dataset, chunk_method="resume")
        if os.environ.get("RAGFLOW_EMBEDDING"):
            ds_payload["embedding_model"] = os.environ["RAGFLOW_EMBEDDING"]
        ds = call("POST", "/datasets", json=ds_payload)
        ds_id = ds["id"] if isinstance(ds, dict) else ds[0]["id"]
        step("dataset_created", dataset_id=ds_id)

        files = sorted(Path(args.resume_dir).glob("*.pdf"))
        uploaded, sha_map = [], {}
        for f in files:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            sha_map[f.name] = sha
            with open(f, "rb") as fh:
                d = call("POST", f"/datasets/{ds_id}/documents",
                         files={"file": (f.name, fh, "application/pdf")})
            doc = d[0] if isinstance(d, list) else d
            uploaded.append((f.name, doc["id"]))
        step("uploaded", count=len(uploaded), elapsed_s=round(time.time() - t0, 1))

        doc_ids = [i for _, i in uploaded]
        call("POST", f"/datasets/{ds_id}/chunks", json={"document_ids": doc_ids})
        step("parse_triggered", count=len(doc_ids))

        pending, done, t_parse = set(doc_ids), {}, time.time()
        while pending and time.time() - t_parse < 3600:
            docs = call("GET", f"/datasets/{ds_id}/documents?page=1&page_size=100")
            rows = docs["docs"] if isinstance(docs, dict) else docs
            for row in rows:
                if row["id"] in pending and row["run"] in ("DONE", "FAIL", "CANCEL"):
                    done[row["id"]] = row["run"]
                    pending.discard(row["id"])
            if pending:
                time.sleep(5)
        step("parse_finished", done=done, elapsed_s=round(time.time() - t_parse, 1))

        with open(out / f"{args.prefix}_import.jsonl", "w", encoding="utf-8") as fh:
            for name, did in uploaded:
                fh.write(json.dumps(dict(filename=name, document_id=did,
                                         sha256=sha_map[name], run=done.get(did)),
                                    ensure_ascii=False) + "\n")

    with open(out / f"{args.prefix}_retrieval.jsonl", "w", encoding="utf-8") as fh:
        for qname, qtext in QUERIES:
            t = time.time()
            data = call("POST", "/retrieval", json=dict(
                question=qtext, dataset_ids=[ds_id], top_k=20,
                similarity_threshold=0.1, keyword=True))
            chunks = data.get("chunks", []) if isinstance(data, dict) else data
            for c in chunks:
                fh.write(json.dumps(dict(query=qname, question=qtext,
                                         document_id=c.get("document_id"),
                                         docnm_kwd=c.get("document_keyword") or c.get("docnm_kwd"),
                                         similarity=c.get("similarity"),
                                         vector_sim=c.get("vector_similarity"),
                                         term_sim=c.get("term_similarity"),
                                         content=c.get("content", "")[:400]),
                                    ensure_ascii=False) + "\n")
            step("retrieval", query=qname, hits=len(chunks),
                 elapsed_s=round(time.time() - t, 2))

    (out / f"{args.prefix}_run_log.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in log), encoding="utf-8")
    print(f"\nB0 基线完成,产物在 {out}")


if __name__ == "__main__":
    main()
