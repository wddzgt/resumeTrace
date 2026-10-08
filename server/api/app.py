"""ResumeTrace 配套 API(规格第 9 节)。本地会话:Header X-RT-Token 校验。"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..db import store
from ..search.service import run_search

logger = logging.getLogger("resumetrace.api")

CFG = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs" / "resumetrace.yaml").read_text())
BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
RF_KEY = os.environ.get("RAGFLOW_API_KEY", "")
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
CHAT_MODEL = os.environ.get("RAGFLOW_CHAT_MODEL", "deepseek-v4.1-flash")
PROVIDER = os.environ.get("RAGFLOW_PROVIDER", "OpenAI-API-Compatible")
INSTANCE = os.environ.get("RAGFLOW_INSTANCE", "dashscope")
TOKEN = os.environ.get("RESUMETRACE_TOKEN", "local-dev")
if TOKEN == "local-dev":
    import warnings as _w
    _w.warn("RESUMETRACE_TOKEN 使用默认值 local-dev,生产环境请设置环境变量", stacklevel=2)

_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

app = FastAPI(title="ResumeTrace")
_imports: dict[str, dict] = {}
_search_progress: dict[str, dict] = {}
_lock = threading.Lock()


def _auth(request: Request):
    if request.headers.get("x-rt-token", "") != TOKEN:
        raise HTTPException(401, {"error_code": "AUTH", "message": "bad token", "details": {}})


def _live(s: dict) -> dict:
    """running 只有在本次进程的进度表里才算真在跑;重启遗留的行不冒充任务(否则 UI 卡在空列表)。"""
    live = bool(s.get("search_id")) and s.get("search_id") in _search_progress
    out = {**s, "live": live}
    if s.get("status") == "running" and not live:
        out["status"] = "interrupted: 服务重启,该任务已中止"
    return out


def _rf_headers():
    return {"Authorization": f"Bearer {RF_KEY}"}


def _llm_call(system: str, user: str) -> str:
    msg = f"{system}\n\n---\n用户输入原文:\n{user}" if system else user
    last = None
    for attempt in range(3):  # 评测链并发时单次调用可能超 300s,重试两次
        t0 = time.monotonic()
        try:
            r = requests.post(f"{BASE}/api/v1/providers/{PROVIDER}/instances/{INSTANCE}/models/{CHAT_MODEL}",
                              headers={**_rf_headers(), "Content-Type": "application/json"},
                              json={"message": msg, "thinking": False}, timeout=600)
            body = r.json()
            elapsed_ms = (time.monotonic() - t0) * 1000
            if body.get("code") != 0:
                last = f"code={body.get('code')} {str(body)[:120]}"
                logger.warning("LLM call failed (attempt %d): %s (%.0fms)", attempt + 1, last, elapsed_ms)
                continue
            ans = body["data"]["answer"]
            usage = body["data"].get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
            logger.info("LLM call ok: model=%s prompt_tok=%d completion_tok=%d total_tok=%d (%.0fms)",
                       CHAT_MODEL, prompt_tokens, completion_tokens, total_tokens, elapsed_ms)
            if isinstance(ans, str) and ans.lstrip().startswith("**ERROR**"):
                last = ans[:160]
                if "QUOTA_EXCEEDED" in ans or "exhausted" in ans.lower():
                    logger.error("LLM quota exceeded: %s", last)
                    break  # 账号级额度用完,重试只是白等几分钟
                logger.warning("LLM returned error (attempt %d): %s", attempt + 1, last)
                continue
            return ans
        except (requests.RequestException, ValueError) as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            last = str(e)[:160]
            logger.warning("LLM call exception (attempt %d): %s (%.0fms)", attempt + 1, last, elapsed_ms)
    logger.error("LLM call exhausted all retries: %s", last)
    raise HTTPException(502, {"error_code": "LLM", "message": f"模型调用失败: {last}", "details": {}})


def _verdict_text(rep: dict) -> str:
    items = rep.get("items", [])
    req = [i for i in items if i.get("category") in ("required", "search_intent")]
    sup = sum(1 for i in req if i.get("state") == "SUPPORTED")
    pend_req = sum(1 for i in req if i.get("state") == "NEEDS_REVIEW")
    pend_other = sum(1 for i in items if i.get("state") == "NEEDS_REVIEW"
                     and i.get("category") not in ("required", "search_intent"))
    pending_total = pend_req + pend_other
    if any(i.get("state") == "CONFLICTING" for i in items):
        return "原文有冲突,先人工核对再决定"
    if req and sup == len(req) and not pend_req:
        return f"建议进入复核:硬性要求 {sup}/{len(req)} 条都有原文证据"
    if sup > 0:
        return f"可进入复核,先确认 {pending_total + len(req) - sup - pend_req} 处(硬性要求 {sup}/{len(req)} 条有原文证据)"
    return "建议暂缓:硬性要求缺少原文证据"


def _note_label(source_type: str) -> str:
    return {"候选人自述": "候选人自述(非原始简历证明)",
            "顾问观察": "顾问观察",
            "外部材料": "外部材料(未背景调查证实)"}.get(source_type, source_type)


def _err(code, message, details=None, status=400):
    raise HTTPException(status, {"error_code": code, "message": message, "details": details or {}})


class ImportReq(BaseModel):
    path: str
    dataset_id: str


class SearchReq(BaseModel):
    dataset_id: str
    query_text: str
    input_type: str = "auto"
    source_url: str | None = None
    limit: int = 20


class RefineReq(BaseModel):
    criteria: list[dict]
    base_search_id: str
    query_text: str | None = None
    limit: int = 20


class NoteReq(BaseModel):
    text: str
    source_type: str
    criterion_id: str | None = None
    author: str = "local"
    search_id: str | None = None


def _validate_id(value: str, field: str = "id"):
    if not value or not _ID_RE.match(value):
        _err("ARG", f"{field} 格式非法:只允许字母数字下划线连字符,1-128字符")


def _safe_dir(path: str) -> Path:
    allowed = [Path(p).expanduser().resolve() for p in CFG["allowed_import_dirs"]]
    p = Path(path).expanduser().resolve()
    if not any(p == d or d in p.parents for d in allowed):
        _err("PATH_ESCAPE", "路径不在允许目录内", {"path": path})
    return p


@app.get("/v1/config")
def get_config(request: Request):
    """返回允许的导入目录列表及各目录下 PDF 文件数,供前端简历库选择器使用。"""
    _auth(request)
    dirs = []
    for d in CFG.get("allowed_import_dirs", []):
        p = Path(d).expanduser()
        pdf_count = len(list(p.glob("*.pdf"))) if p.is_dir() else 0
        dirs.append({"path": str(p), "pdf_count": pdf_count, "exists": p.is_dir()})
    return {"allowed_import_dirs": dirs, "local_pdf_dir": CFG.get("local_pdf_dir", "")}


@app.post("/v1/libraries/import")
def library_import(req: ImportReq, request: Request):
    _auth(request)
    root = _safe_dir(req.path)
    files = sorted(root.glob("*.pdf"))[: CFG["import_limits"]["max_files_per_batch"]]
    task_id = store.new_id("imp")
    _imports[task_id] = {"status": "running", "files": [], "started": time.time()}

    def work():
        try:
            for f in files:
                if f.stat().st_size > CFG["import_limits"]["max_file_mb"] * 1024 * 1024:
                    _imports[task_id]["files"].append({"name": f.name, "status": "FAIL",
                                                       "reason": "too_large"})
                    continue
                sha = hashlib.sha256(f.read_bytes()).hexdigest()
                try:
                    with open(f, "rb") as fh:
                        r = requests.post(f"{BASE}/api/v1/datasets/{req.dataset_id}/documents",
                                          headers=_rf_headers(),
                                          files={"file": (f.name, fh, "application/pdf")}, timeout=120)
                    body = r.json()
                    ok = body.get("code") == 0
                    _imports[task_id]["files"].append(
                        {"name": f.name, "sha256": sha, "status": "OK" if ok else "FAIL",
                         "document_id": body.get("data", [{}])[0].get("id") if ok else None,
                         "reason": "" if ok else body.get("message")})
                except Exception as e:
                    _imports[task_id]["files"].append({"name": f.name, "sha256": sha,
                                                       "status": "FAIL", "reason": str(e)[:200]})
            doc_ids = [x["document_id"] for x in _imports[task_id]["files"] if x["status"] == "OK"]
            chunk_ok = False
            if doc_ids:
                try:
                    cr = requests.post(f"{BASE}/api/v1/datasets/{req.dataset_id}/chunks",
                                  headers={**_rf_headers(), "Content-Type": "application/json"},
                                  json={"document_ids": doc_ids}, timeout=120)
                    chunk_ok = cr.status_code == 200
                except Exception as e:
                    logger.warning("Chunk trigger failed for task %s: %s", task_id, e)
            failed_count = sum(1 for x in _imports[task_id]["files"] if x["status"] == "FAIL")
            if failed_count or not chunk_ok:
                _imports[task_id]["status"] = "done_with_errors"
            else:
                _imports[task_id]["status"] = "done"
        except Exception as e:
            logger.exception("Import thread crashed: task=%s", task_id)
            _imports[task_id]["status"] = f"failed: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"task_id": task_id, "file_count": len(files)}


@app.get("/v1/imports/{task_id}")
def import_status(task_id: str , request: Request):
    _auth(request)
    if task_id not in _imports:
        _err("NOT_FOUND", "任务不存在", status=404)
    return _imports[task_id]


@app.post("/v1/searches")
def create_search(req: SearchReq , request: Request):
    _auth(request)
    if req.limit > CFG["search"]["limit_max"]:
        _err("LIMIT", f"limit 上限 {CFG['search']['limit_max']}")
    if not req.query_text or not req.query_text.strip():
        _err("ARG", "请输入搜索要求或 JD 原文")
    qsha = hashlib.sha256(req.query_text.encode()).hexdigest()
    sid = store.create_search(req.dataset_id, req.query_text, qsha, "auto", req.source_url)
    _search_progress[sid] = {"done": 0, "total": None, "ready": [], "stage": "排队中"}
    logger.info("Search created: sid=%s dataset=%s limit=%d query_len=%d",
               sid, req.dataset_id, req.limit, len(req.query_text))
    _start_search_thread(sid, req.dataset_id, req.query_text, True, req.limit)
    return {"search_id": sid}


def _start_search_thread(sid, dataset_id, qtext, scope_constraints, limit, criteria=None):
    """边跑边出:每个候选人判完立即落库并进入就绪列表,首份报告不用等全量。"""
    prog = _search_progress[sid]

    def emit(cand):
        rid = store.save_report(sid, cand["candidate"], cand["report"])
        analysis = cand.get("report", {}).get("analysis", {})
        prog["ready"].append({"candidate": cand["candidate"], "report_id": rid,
                              "score": cand["score"], "coverage": cand["coverage"],
                              "states": cand.get("states", {}),
                              "verdict_text": _verdict_text(cand["report"]),
                              "analysis_summary": {
                                  "overall_assessment": analysis.get("overall_assessment", ""),
                                  "fit_level": analysis.get("role_fit", {}).get("fit_level", ""),
                                  "next_step": analysis.get("recommendations", {}).get("next_step", ""),
                              }})
        prog["done"] += 1

    def stage(name, info):
        prog["stage"] = {"criteria": "已拆解条件,开始检索简历",
                         "recall": "开始逐人核对原文"}.get(name, name)
        if name == "recall":
            prog["total"] = info.get("total")

    def work():
        try:
            logger.info("Search thread started: sid=%s", sid)
            res = run_search(dataset_id, qtext, TENANT, RF_KEY, _llm_call,
                             scope_constraints=scope_constraints, top_n=limit,
                             chat_model=CHAT_MODEL, criteria=criteria,
                             on_candidate=emit, on_stage=stage)
            if criteria is None:
                store.save_criteria(sid, 1, res["criteria"], res["warnings"])
            prog["total"] = len(res["candidates"])
            if not res["criteria"]:
                store.finish_search(sid, "failed: 未解析出任何结构合法条件,请调整输入")
                prog["error"] = "未解析出任何结构合法条件"
                logger.warning("Search failed: no valid criteria parsed sid=%s", sid)
            else:
                store.finish_search(sid)
                prog["stage"] = "完成"
                logger.info("Search completed: sid=%s candidates=%d criteria=%d",
                           sid, len(res["candidates"]), len(res["criteria"]))
        except Exception as e:  # 可见错误,不静默
            store.finish_search(sid, f"failed: {e}")
            prog["error"] = str(e)
            logger.exception("Search failed with exception: sid=%s", sid)

    threading.Thread(target=work, daemon=True).start()


@app.get("/v1/searches")
def list_searches(request: Request):
    _auth(request)
    from ..db.store import conn as _conn
    with _conn() as c:
        rows = c.execute("SELECT s.search_id,s.status,s.created_at,s.query_text,"
                         "(SELECT COUNT(*) FROM reports r WHERE r.search_id=s.search_id) AS report_count "
                         "FROM searches s ORDER BY s.created_at DESC LIMIT 20").fetchall()
    return [_live(dict(r)) for r in rows]


@app.get("/v1/searches/{sid}")
def search_status(sid: str , request: Request):
    _auth(request)
    row = store.get_search(sid)
    if not row:
        _err("NOT_FOUND", "搜索不存在", status=404)
    s = _live(row)
    prog = _search_progress.get(sid)
    rows = store.list_reports(sid)
    if prog is not None:
        prog = {**prog, "ready": sorted(prog["ready"], key=lambda x: -x["score"])}
    else:
        # 内存进度随服务重启丢失:从 DB 报告组装就绪列表
        import json as _json
        latest = {}
        for r in rows:  # 同一候选人只保留最新版本(笔记会产生新版本)
            cur = latest.get(r["candidate_id"])
            if cur is None or r["version"] > cur["version"]:
                latest[r["candidate_id"]] = r
        ready = []
        for r in latest.values():
            rep = _json.loads(r["report_json"])
            ready.append({"candidate": r["candidate_id"], "report_id": r["report_id"],
                          "score": rep["score"]["score"], "coverage": rep["score"]["coverage_pct"],
                          "states": {it["criterion_id"]: it["state"] for it in rep["items"]},
                          "verdict_text": _verdict_text(rep)})
        ready.sort(key=lambda x: -x["score"])
        prog = {"ready": ready, "total": len(ready), "done": len(ready)}
    return dict(search=s, progress=prog,
                reports=[{"candidate": r["candidate_id"], "version": r["version"],
                          "report_id": r["report_id"]} for r in rows])


@app.post("/v1/searches/{sid}/refine")
def refine(sid: str, req: RefineReq , request: Request):
    _auth(request)
    old = store.get_search(sid)
    if not old:
        _err("NOT_FOUND", "原搜索不存在", status=404)
    qtext = req.query_text or old["query_text"]
    new_sid = store.create_search(old["dataset_id"], qtext,
                                  hashlib.sha256(qtext.encode()).hexdigest(),
                                  "auto", old["source_url"],
                                  scope_constraints=bool(old["scope_constraints"]),
                                  parent_search_id=sid)
    store.save_criteria(new_sid, old["criteria_version"] + 1, req.criteria, [])
    from ..core.contracts import JobCriterion
    crit = []
    for cd in req.criteria:
        try:
            crit.append(JobCriterion(**cd))
        except Exception as e:
            _err("ARG", f"条件结构非法: {e}")
    _search_progress[new_sid] = {"done": 0, "total": None, "ready": [], "stage": "排队中"}
    _start_search_thread(new_sid, old["dataset_id"], qtext, bool(old["scope_constraints"]),
                         min(req.limit, CFG["search"]["limit_max"]), criteria=crit)
    return {"search_id": new_sid, "note": "原搜索与报告保留,可回看"}


@app.get("/v1/reports/{rid}")
def get_report(rid: str , request: Request):
    _auth(request)
    row = store.get_report(rid)
    if not row:
        _err("NOT_FOUND", "报告不存在", status=404)
    import json as _json
    rep = _json.loads(row["report_json"])
    rep.setdefault("report_id", rid)   # 前端据此精确取该报告的证据(跨报告 span_id 会重名)
    if row["source_deleted"]:
        rep["source_status"] = "来源已删除,报告不可再验证"
    return rep


@app.get("/v1/evidence/{span_ref}")
def get_evidence(span_ref: str, dataset_id: str, document_id: str, request: Request):
    """span_ref 形如 docid:spN;返回受控下载地址+页码+矩形+引文+版本哈希。"""
    _auth(request)
    _validate_id(dataset_id, "dataset_id")
    _validate_id(document_id, "document_id")
    from ..core.evidence import es_chunks_for_document, chunks_to_evidence
    doc = requests.get(f"{BASE}/api/v1/datasets/{dataset_id}/documents/{document_id}",
                       headers=_rf_headers(), timeout=120)
    pdf = doc.content
    ver_sha = hashlib.sha256(pdf).hexdigest()
    chs = es_chunks_for_document(TENANT, document_id)
    spans, _ = chunks_to_evidence(chs, type("V", (), {"source_sha256": ver_sha})(), None)
    sp = spans.get(span_ref.split(":")[-1])
    if not sp:
        _err("NOT_FOUND", "证据不存在", status=404)
    return dict(download_url=f"{BASE}/api/v1/datasets/{dataset_id}/documents/{document_id}",
                page=sp.page_index, rects=[r.model_dump() for r in sp.rects],
                quote=sp.quote, quote_hash=sp.quote_hash, source_sha256=ver_sha,
                extraction_mode=sp.extraction_mode, status=sp.status)


@app.get("/v1/evidence/{span_ref}/pdf")
def evidence_pdf(span_ref: str, dataset_id: str, document_id: str, request: Request):
    """受控 PDF 代理:RAGFlow key 只存服务端,浏览器不接触(规格第 9 节)。"""
    _auth(request)
    _validate_id(dataset_id, "dataset_id")
    _validate_id(document_id, "document_id")
    r = requests.get(f"{BASE}/api/v1/datasets/{dataset_id}/documents/{document_id}",
                     headers=_rf_headers(), timeout=120)
    if r.status_code != 200:
        _err("UPSTREAM", "原件下载失败", status=502)
    from fastapi.responses import Response
    safe_name = re.sub(r'[^\w\-.]', '_', document_id)
    return Response(content=r.content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe_name}.pdf"'})


def _expr_text(node) -> str:
    """条件表达式树 → 扁平文字,给证据定位当关键词来源。"""
    if not isinstance(node, dict):
        return ""
    parts = [node.get("predicate") or "", node.get("value") or ""]
    parts += [_expr_text(ch) for ch in (node.get("children") or [])]
    return " ".join(p for p in parts if p)


@app.get("/v1/evidence/{span_ref}/png")
def evidence_png(span_ref: str, request: Request, dataset_id: str = None,
                 document_id: str = None, filename: str = None, report_id: str = None,
                 evidence_quote: str = None):
    """服务端渲染证据页 PNG(pypdfium2 + 高亮矩形),浏览器零字体依赖。带磁盘缓存。
    优先用本地 PDF 文件,回退 RAGFlow 下载。"""
    _auth(request)
    if dataset_id:
        _validate_id(dataset_id, "dataset_id")
    if document_id:
        _validate_id(document_id, "document_id")
    if report_id:
        _validate_id(report_id, "report_id")
    import hashlib
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    from fastapi.responses import Response as _Resp

    cache = Path(__file__).resolve().parents[2] / "data" / "page_png"
    cache.mkdir(parents=True, exist_ok=True)

    span_id = span_ref.split(":")[-1]
    like_pat = f'%"span_id": "{span_id}"%'
    with store.conn() as c:
        # span_id 只在单份简历内唯一:优先按打开中的 report_id 精确取,否则才全文回退
        if report_id:
            row = c.execute("SELECT report_json FROM reports WHERE report_id=? LIMIT 1",
                            (report_id,)).fetchone()
            if not row or f'"span_id": "{span_id}"' not in (row["report_json"] or ""):
                row = c.execute(
                    "SELECT report_json FROM reports WHERE report_json LIKE ? LIMIT 1",
                    (like_pat,)).fetchone()
        elif dataset_id:
            row = c.execute(
                "SELECT report_json FROM reports WHERE search_id IN "
                "(SELECT search_id FROM searches WHERE dataset_id=?) "
                "AND report_json LIKE ? LIMIT 1",
                (dataset_id, like_pat)
            ).fetchone()
        elif filename:
            row = c.execute(
                "SELECT report_json FROM reports WHERE report_json LIKE ? "
                "AND report_json LIKE ? LIMIT 1",
                (like_pat, f'%"filename": "{filename}"%')
            ).fetchone()
        else:
            row = c.execute(
                "SELECT report_json FROM reports WHERE report_json LIKE ? LIMIT 1",
                (like_pat,)
            ).fetchone()
    if not row:
        _err("NOT_FOUND", "证据不存在", status=404)

    import json as _json
    rep = _json.loads(row["report_json"])
    # 同一 span 可被不同条件引用不同句子:带 evidence_quote 时精确取那一条
    cands = [(it, ci) for it in rep.get("items", []) for ci in it.get("citations", [])
             if ci.get("span_id") == span_id]
    pair = next((p for p in cands
                 if evidence_quote and (p[1].get("evidence_quote") or "") == evidence_quote),
                cands[0] if cands else None)
    if not pair:
        _err("NOT_FOUND", "证据不存在", status=404)
    item_data, sp_data = pair
    # 这条引用服务于哪条要求:整段坐标收窄到句子时,只有条件里的关键词能决定是哪句
    req_text = " ".join(item_data.get("input_fragments") or []).strip()
    if not req_text:
        req_text = " ".join(_expr_text(c.get("expression"))
                            for c in rep.get("criteria", [])
                            if c.get("criterion_id") == item_data.get("criterion_id"))

    page_index = sp_data["page"] - 1
    rects = sp_data["rects"]
    cite_quote = (sp_data.get("evidence_quote") or "").strip()
    quote_hash = hashlib.sha256(
        f"{sp_data.get('quote') or ''}|{cite_quote}|{req_text}".encode()).hexdigest()
    cand_filename = filename or rep.get("candidate", {}).get("filename", "")
    cache_key_src = f"{cand_filename}|{page_index}|{quote_hash}"
    key = hashlib.sha256(cache_key_src.encode()).hexdigest()[:16]
    png = cache / f"{key}.png"

    if not png.exists():
        pdf_binary = None
        local_dir = Path(CFG.get("local_pdf_dir", "")) if CFG.get("local_pdf_dir") else None
        if local_dir and local_dir.exists() and cand_filename:
            candidate_path = (local_dir / cand_filename).resolve()
            if local_dir.resolve() in candidate_path.parents or candidate_path == local_dir.resolve():
                if candidate_path.exists():
                    pdf_binary = candidate_path.read_bytes()
        if pdf_binary is None and document_id:
            r = requests.get(f"{BASE}/api/v1/datasets/{dataset_id}/documents/{document_id}",
                             headers=_rf_headers(), timeout=120)
            if r.status_code == 200:
                pdf_binary = r.content
        if pdf_binary is None:
            _err("NO_PDF", f"找不到 PDF 文件: {cand_filename}", status=502)

        pdf = pdfium.PdfDocument(io.BytesIO(pdf_binary))
        try:
            if page_index < 0 or page_index >= len(pdf):
                _err("ARG", f"页码索引 {page_index} 超出范围(共 {len(pdf)} 页)", status=400)
            page = pdf[page_index]
            img = page.render(scale=2.0).to_pil().convert("RGB")
            sx = img.size[0] / page.get_width()
            sy = img.size[1] / page.get_height()
            page_height = page.get_height()
            # 证据框优先落到"真正支撑结论的那一句":RAGFlow 只给经历抬头存了坐标,
            # 正文句(如 · 后端技术|Spring Boot…)靠 pdfplumber 现算行矩形
            draw_rects = rects
            from ..core.evidence import line_rects_for_quote, narrow_rects_to_keywords
            if cite_quote and cite_quote != (sp_data.get("quote") or ""):
                located = line_rects_for_quote(pdf_binary, page_index, cite_quote)
                if located:
                    draw_rects = located
                logger.info("Evidence line locate: span=%s located=%d whole=%d quote=%.40s",
                            span_id, len(located), len(rects), cite_quote)
            else:
                # 模型没摘抄到具体句子时,span 坐标是**整段**(抬头+正文全框住),满屏红框等于没框
                narrowed = narrow_rects_to_keywords(pdf_binary, page_index, rects, req_text)
                if narrowed:
                    draw_rects = narrowed
                logger.info("Evidence rect narrow: span=%s narrowed=%d whole=%d req=%.40s",
                            span_id, len(narrowed), len(rects), req_text)
            logger.info(f"Rendering evidence PNG: img_size={img.size}, page_index={page_index}, num_rects={len(draw_rects)}, scale=({sx:.4f},{sy:.4f})")
            d = ImageDraw.Draw(img, "RGBA")
            for i, rc in enumerate(draw_rects):
                if rc.get("page", page_index) != page_index:
                    continue
                # RAGFlow pdfplumber 返回图像坐标 (左上角原点,y 向下),单位 PDF points
                # 直接用 scale 转为像素,无需翻转
                x0 = rc["x0"] * sx
                y0 = rc["top"] * sy
                x1 = rc["x1"] * sx
                y1 = rc["bottom"] * sy
                logger.info(f"  rect[{i}]: pdf=({rc['x0']},{rc['top']},{rc['x1']},{rc['bottom']}) -> pixels=({x0},{y0},{x1},{y1})")
                d.rectangle([x0, y0, x1, y1], fill=(255, 220, 0, 80), outline=(200, 40, 40, 240), width=3)
            img.save(png, "PNG")
        finally:
            pdf.close()
    return _Resp(content=png.read_bytes(), media_type="image/png",
                 headers={"Cache-Control": "no-store"})


@app.get("/v1/resume/page/png")
def resume_page_png(filename: str, page: int = 1, request: Request = None):
    """渲染简历指定页 PNG(无高亮),用于无引证时展示原文。"""
    _auth(request)
    import pypdfium2 as pdfium
    from PIL import Image
    from fastapi.responses import Response as _Resp

    cache = Path(__file__).resolve().parents[2] / "data" / "page_png"
    cache.mkdir(parents=True, exist_ok=True)

    cache_key_src = f"{filename}|{page - 1}|plain"
    key = hashlib.sha256(cache_key_src.encode()).hexdigest()[:16]
    png = cache / f"{key}.png"

    if not png.exists():
        pdf_binary = None
        local_dir = Path(CFG.get("local_pdf_dir", "")) if CFG.get("local_pdf_dir") else None
        if local_dir and local_dir.exists() and filename:
            candidate_path = (local_dir / filename).resolve()
            if not (local_dir.resolve() in candidate_path.parents or candidate_path == local_dir.resolve()):
                _err("PATH_ESCAPE", "路径不安全", status=400)
            if candidate_path.exists():
                pdf_binary = candidate_path.read_bytes()
        if pdf_binary is None:
            _err("NO_PDF", f"找不到 PDF 文件：{filename}", status=502)

        pdf = pdfium.PdfDocument(io.BytesIO(pdf_binary))
        try:
            if page < 1 or page > len(pdf):
                _err("ARG", f"页码 {page} 超出范围(共 {len(pdf)} 页)", status=400)
            page_obj = pdf[page - 1]
            img = page_obj.render(scale=2.0).to_pil().convert("RGB")
            img.save(png, "PNG")
        finally:
            pdf.close()
    return _Resp(content=png.read_bytes(), media_type="image/png",
                 headers={"Cache-Control": "no-store"})


@app.post("/v1/candidates/{candidate_id}/notes")
def add_note(candidate_id: str, req: NoteReq , request: Request):
    _auth(request)
    if req.source_type not in ("候选人自述", "顾问观察", "外部材料"):
        _err("ARG", "source_type 非法")
    nid, ver = store.add_note(candidate_id, req.text, req.source_type, req.author, req.criterion_id)
    import json as _json
    import time as _time
    sid = req.search_id
    if not sid:
        with store.conn() as c:
            row = c.execute("SELECT search_id FROM reports WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1",
                            (candidate_id,)).fetchone()
        sid = row["search_id"] if row else None
    new_rid = None
    if sid:
        reps = [r for r in store.list_reports(sid) if r["candidate_id"] == candidate_id]
        if reps:
            base = _json.loads(reps[-1]["report_json"])
            base.setdefault("notes", []).append(dict(
                note_id=nid, source_type=req.source_type, label=_note_label(req.source_type),
                text=req.text, recorded_at=_time.time()))
            new_rid = store.save_report(sid, candidate_id, base)
    return {"note_id": nid, "report_version": ver, "report_id": new_rid,
            "label": _note_label(req.source_type)}


@app.get("/v1/candidates/{candidate_id}/reports")
def candidate_reports(candidate_id: str, request: Request):
    _auth(request)
    with store.conn() as c:
        rows = c.execute("SELECT report_id, search_id, version, created_at FROM reports "
                         "WHERE candidate_id=? ORDER BY version", (candidate_id,)).fetchall()
    return [dict(r) for r in rows]
