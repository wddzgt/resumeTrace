"""一次性脚本:给历史报告里"部分满足/待复核"却没有引用的条目补上已定位原文。

两个根因都已修在代码里,但已入库的报告是当次判定的产物,补一遍才能立刻看到:
1) server/search/orchestrator.py —— 判定只在条件完全成立时收集 source span,
   部分成立(AND 只满足一枝、跨经历拼接)把已定位原文丢掉 → UI 显示"未找到可定位原文";
2) RAGFlow resume 解析只给"经历抬头行"存了 position_int,正文里真正支撑结论的那句
   (如 · 后端技术|Spring Boot…)没有坐标 → 证据框只能框到抬头。
   渲染端改为按 evidence_quote 用 pdfplumber 现算行矩形(见 evidence.line_rects_for_quote)。

本脚本只补 citations/evidence_quote,不改 state/reason/score;
模型配额恢复后重跑搜索,判定层会自己产出同样的引用。

补出来的引用必须真的对上条件:关键词里既不含"开发/系统/模块"这类简历通用词
(否则要求"支付/风控"会圈出"独立完成前后端开发"),也不保留解析器的英文小标题
("Project Description/Responsibilities（…）:"),简历页面上没有这一串。

用法: python3 scripts/backfill_partial_citations.py [--dry-run] [--force] [--dedupe] [--reword] [--search srch-xxxx]
      --dedupe 只去掉指向同一块坐标的重复引用(一个 chunk 会拆出 sp4/sp5 两个同坐标 span)
      --reword 只重算"要求"文案(偏移边界吸附)与待确认话术(改成点名岗位原文的白话)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypdf
import requests

from server.core.contracts import (CriterionState, JobCriterion, ResumeVersion)
from server.core.evidence import (_norm_quote, chunks_to_evidence,
                                  es_chunks_for_document, requirement_keywords)
from server.jd.parser import locate_clause, requirement_fragments
from server.search.orchestrator import build_review_question

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "resumetrace.db"
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
# 补引用要读简历原 PDF:自己的目录用 LOCAL_PDF_DIR 指过来,默认用导入时留在仓库里的副本;
# 只跑 --reword / --dedupe 不碰 PDF
PDF_DIRS = ([Path(os.environ["LOCAL_PDF_DIR"])] if os.environ.get("LOCAL_PDF_DIR") else []) \
    + [ROOT / "deploy" / "ragflow-logs" / "resumes"]
DOC_URL_RE = re.compile(r"/datasets/([a-f0-9]+)/documents/([a-f0-9]+)")
_EXPERIENCE_KINDS = ("work", "project", "education")
_FILL_STATES = ("PARTIAL", "NEEDS_REVIEW", "CONFLICTING")


def leaf_texts(crit: JobCriterion) -> list[str]:
    leaves, stack = [], [crit.expression]
    while stack:
        n = stack.pop()
        if n.op:
            stack.extend(n.children)
        elif n.predicate or n.value:
            leaves.append(" ".join(p for p in (n.predicate, n.value) if p))
    return leaves


# 解析器给 chunk 每行加了英文小标题("Project Description/Responsibilities（在线书店系统）: …"),
# 简历页面上并没有这一串,留着它 HR 会以为引文对不上
_LABEL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z &/.'-]*(（[^）]*）)?[:：]\s*")
_TENANT_FOOTER = re.compile(r"^\[Name:")


def _clean_line(ln: str) -> str:
    ln = ln.lstrip("·-—* \t")
    if _TENANT_FOOTER.match(ln):
        return ""
    return _LABEL_PREFIX.sub("", ln).strip()


def evidence_line(chunk_text: str, kws: list[str]) -> str:
    """取 chunk 里命中关键词最多的那一行(去掉列表符号),作为定位句。"""
    best, best_hits = "", 0
    for raw in (chunk_text or "").splitlines():
        ln = _clean_line(raw)
        if not ln:
            continue
        blob = _norm_quote(ln).lower()
        n = sum(1 for k in kws if _norm_quote(k).lower() in blob)
        if n > best_hits:
            best, best_hits = ln, n
    return best


def find_pdf(filename: str):
    for d in PDF_DIRS:
        if (d / filename).exists():
            return d / filename
    return None


def doc_ids_from(rep: dict, filename: str=""):
    """优先从已有引用的 download_url 反解 (dataset_id, document_id);
    整份报告都没引用时,按文件名直查 ES 拿 doc_id。"""
    for item in rep.get("items", []):
        for ci in item.get("citations", []):
            m = DOC_URL_RE.search(ci.get("download_url") or "")
            if m:
                return m.group(1), m.group(2)
    if not filename:
        return None, None
    from server.core.evidence import ES_AUTH, ES_URL
    q = {"size": 1, "_source": ["doc_id", "kb_id"],
         "query": {"term": {"docnm_kwd": filename}}}
    try:
        hits = requests.get(f"{ES_URL}/ragflow_{TENANT}/_search", auth=ES_AUTH,
                            json=q, timeout=30).json()["hits"]["hits"]
    except Exception as e:
        print(f"  ES 文件名反查失败 {filename}: {e}")
        return None, None
    if not hits:
        return None, None
    src = hits[0]["_source"]
    return src.get("kb_id"), src.get("doc_id")


def dedupe_rep(rep: dict) -> int:
    """去掉指向同一块坐标的重复引用:一个 chunk 会拆出 sp4/sp5 两个同坐标 span,
    6 个"看原文"按钮其实只有 3 处地方。与 render_report 里的同一条规则,用于刷历史数据。"""
    dropped = 0
    for it in rep.get("items", []):
        seen, keep = set(), []
        for ci in it.get("citations") or []:
            geom = (ci.get("page"), tuple(round(v) for rc in ci.get("rects") or []
                                          for v in (rc.get("x0"), rc.get("top"),
                                                    rc.get("x1"), rc.get("bottom"))))
            if geom in seen:
                dropped += 1
                continue
            seen.add(geom)
            keep.append(ci)
        it["citations"] = keep
    return dropped


def reword_rep(rep: dict) -> int:
    """重算"要求"文案与待确认话术,只用已存的 raw_text + input_spans。

    两条老毛病:偏移是模型给的,常切在词中间(ySQL,有支付或风控系统开 → MySQL 掉了
    "Me"、句子断腰);话术拼的是内部条件叶子,HR 看不出跟左栏那条要求有啥关系。
    """
    raw = (rep.get("query") or {}).get("raw_text") or ""
    crit_by_id = {c["criterion_id"]: c for c in rep.get("criteria", [])}
    changed = 0
    for it in rep.get("items", []):
        c = crit_by_id.get(it.get("criterion_id"))
        frags = requirement_fragments(JobCriterion(**c), raw) if c else []
        if not frags:
            # 老报告连 criteria 都没存:拿当存的片段回原文定位,顺带把切歪的边界还原
            frags = [f for f in (locate_clause(raw, x) for x in it.get("input_fragments") or []) if f]
        if not frags:
            continue          # 原文里捞不到这句就不改,拿内部标签充数更容易被当成乱关联
        old_q = it.get("review_question")
        new_q = build_review_question(CriterionState(it["state"]), "、".join(frags),
                                      duration=bool(c and c.get("min_duration_months")))
        if it["input_fragments"] != frags or old_q != new_q:
            it["input_fragments"] = frags
            it["review_question"] = new_q
            changed += 1
    rep["review_questions"] = [dict(criterion_id=i["criterion_id"], question=i["review_question"])
                               for i in rep.get("items", []) if i.get("review_question")]
    return changed


def main():
    dry = "--dry-run" in sys.argv
    # --force:重刷本脚本补过的条目(按 scope_kind 标记识别),用于调整引用排序
    force = "--force" in sys.argv
    only_search = sys.argv[sys.argv.index("--search") + 1] if "--search" in sys.argv else None

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    sql, args = "SELECT report_id, search_id, report_json FROM reports", ()
    if only_search:
        sql, args = sql + " WHERE search_id=?", (only_search,)
    rows = conn.execute(sql, args).fetchall()
    print(f"reports: {len(rows)}")

    if "--dedupe" in sys.argv or "--reword" in sys.argv:   # 只做文本层清洗,不动引用
        reword = "--reword" in sys.argv
        n, touched = 0, 0
        for row in rows:
            rep = json.loads(row["report_json"])
            delta = reword_rep(rep) if reword else dedupe_rep(rep)
            if delta:
                touched += 1
                if not dry:
                    conn.execute("UPDATE reports SET report_json=? WHERE report_id=?",
                                 (json.dumps(rep, ensure_ascii=False), row["report_id"]))
            n += delta
        if dry:
            conn.rollback()
        else:
            conn.commit()
        conn.close()
        label = ("重算要求文案/待确认话术的条目" if reword else "去掉重复引用")
        print(f"{'[dry-run] ' if dry else ''}{label}: {n}(报告 {touched} 份)")
        return

    cache: dict[str, dict] = {}
    touched, added = 0, 0

    for row in rows:
        rep = json.loads(row["report_json"])
        filename = (rep.get("candidate") or {}).get("filename", "")

        def needs_fill(it):
            if it.get("state") not in _FILL_STATES:
                return False
            cites = it.get("citations") or []
            if not cites:
                return True
            return force and all("evidence_quote" in c for c in cites)

        todo = [it for it in rep.get("items", []) if needs_fill(it)]
        if not todo:
            continue
        dataset_id, doc_id = doc_ids_from(rep, filename)
        if not doc_id or not filename:
            print(f"  skip {row['report_id']}: 整份报告都没有引用,反解不到文档")
            continue

        if doc_id not in cache:
            bundle: dict = {}
            pdf_path = find_pdf(filename)
            if pdf_path:
                binary = pdf_path.read_bytes()
                chunks = es_chunks_for_document(TENANT, doc_id)
                pages = [(p.extract_text() or "") for p in pypdf.PdfReader(str(pdf_path)).pages]
                ver = ResumeVersion(source_sha256=hashlib.sha256(binary).hexdigest(),
                                    candidate_id=filename, filename=filename,
                                    dataset_id=dataset_id, document_id=doc_id,
                                    ingested_at="backfill", parser_version="backfill",
                                    status="ready")
                spans, _ = chunks_to_evidence(chunks, ver, pages)
                # chunk 全文才有关键词:span.quote 只是经历抬头
                bundle = dict(ver=ver, spans=spans, chunks=[
                    dict(text=ck.get("content_with_weight") or "",
                         refs=[r for r in (ck.get("resume_source_ref_id_kwd") or [])
                               if r in spans and spans[r].status == "located"],
                         kind=ck.get("resume_scope_kind_kwd") or "")
                    for ck in chunks])
            cache[doc_id] = bundle
        bundle = cache[doc_id]
        if not bundle:
            print(f"  skip {row['report_id']}: 找不到 {filename} 或 ES 无证据")
            continue
        spans, chunks, ver = bundle["spans"], bundle["chunks"], bundle["ver"]

        crit_by_id = {c["criterion_id"]: c for c in rep.get("criteria", [])}
        changed = False
        for it in todo:
            c = crit_by_id.get(it["criterion_id"])
            if not c:
                continue

            picked, seen = [], set() if force else {x.get("span_id") for x in it.get("citations", [])}
            for leaf in leaf_texts(JobCriterion(**c)):
                kws = requirement_keywords(leaf)
                if not kws:
                    continue
                scored = []
                for ck in chunks:
                    if not ck["refs"]:
                        continue
                    blob = _norm_quote(ck["text"]).lower()
                    hits = [k for k in kws if _norm_quote(k).lower() in blob]
                    if hits:
                        scored.append((len(hits), ck, hits))
                # 命中关键词多的经历优先;同样相关时经历类胜过技能清单
                scored.sort(key=lambda e: (-e[0], 0 if e[1]["kind"] in _EXPERIENCE_KINDS else 1))
                for _n, ck, hits in scored[:2]:
                    sid = ck["refs"][0]
                    if sid in seen:
                        continue
                    sp = spans[sid]
                    line = evidence_line(ck["text"], hits[0])
                    picked.append(dict(span_id=sid, page=sp.page_index + 1,
                                       rects=[r.model_dump() for r in sp.rects],
                                       quote=sp.quote, extraction_mode=sp.extraction_mode,
                                       status=sp.status,
                                       evidence_quote=line or sp.quote,
                                       scope_kind=ck["kind"] or "unknown",
                                       download_url=f"{os.environ.get('RAGFLOW_BASE_URL', 'http://localhost:9380')}"
                                                    f"/api/v1/datasets/{ver.dataset_id}/documents/{ver.document_id}",
                                       label="OCR 文本(扫描件)" if sp.extraction_mode == "OCR" else "原文"))
                    seen.add(sid)
                    added += 1
                    print(f"  {row['report_id']} {it['criterion_id']}({it['state']}): "
                          f"{sid} 第{sp.page_index + 1}页 {hits} → {(line or '(整段)')[:60]}")
            if picked:
                # 经历类证据排在前面:技能清单里的声称只是兜底,不该当第一条给 HR 看
                picked.sort(key=lambda ci: 0 if ci["scope_kind"] in _EXPERIENCE_KINDS else 1)
                it["citations"] = picked
                changed = True
            elif force:
                # 关键词收紧后配不上任何原文:清掉上次回填的泛泛引用,宁可显示"未找到"
                dropped = len(it.get("citations") or [])
                it["citations"] = []
                changed = True
                print(f"  {row['report_id']} {it['criterion_id']}({it['state']}): "
                      f"关键词收紧后无匹配,清空 {dropped} 条泛泛引用")
        if changed and not dry:
            conn.execute("UPDATE reports SET report_json=? WHERE report_id=?",
                         (json.dumps(rep, ensure_ascii=False), row["report_id"]))
            touched += 1

    if dry:
        conn.rollback()
    else:
        conn.commit()
    conn.close()
    print(f"{'[dry-run] ' if dry else ''}更新报告: {touched}, 新增引用: {added}")


if __name__ == "__main__":
    main()
