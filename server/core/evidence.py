"""证据读取服务:把 RAGFlow chunk 的证据字段还原为契约对象并校验(规格 6/7.1)。

- 原文位置只信 chunk 的 position_int(解析期行号映射的真实矩形);
- extraction_mode 由服务端推断:该页有文本层=metadata,无文本层=OCR;
- 引文必须能在指定 PDF 页的文本中(标准化后)匹配,否则 span 降为 unlocated,
  不得作为"明确支持"的证据(规格 0.5)。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from typing import Optional

import requests

from .contracts import (ExperienceScope, Rect, ResumeVersion, ScopeKind,
                        SourceSpan, _norm)

# RAGFlow 的 chunk/检索 API 对返回字段做白名单,自定义证据字段会被裁掉;
# 证据读取直连 Elasticsearch(规格第 5 节:ES 即文档检索引擎)。
ES_URL = os.environ.get("RAGFLOW_ES_URL", "http://localhost:1200")
ES_AUTH = ("elastic", os.environ.get("RAGFLOW_ES_PASSWORD", "infini_rag_flow"))
if os.environ.get("RAGFLOW_ES_PASSWORD") is None:
    import warnings as _w
    _w.warn("RAGFLOW_ES_PASSWORD 未设置,使用默认密码;生产环境请设置环境变量", stacklevel=2)

_TENANT_RE = __import__("re").compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _validate_tenant(tenant_id: str):
    if not tenant_id or not _TENANT_RE.match(tenant_id):
        raise ValueError(f"tenant_id 格式非法: {tenant_id}")


def es_chunks_for_document(tenant_id: str, document_id: str, size: int = 300) -> list[dict]:
    _validate_tenant(tenant_id)
    q = {"size": size, "query": {"term": {"doc_id": document_id}}}
    r = requests.get(f"{ES_URL}/ragflow_{tenant_id}/_search", auth=ES_AUTH,
                     json=q, timeout=60)
    r.raise_for_status()
    return [{**h["_source"], "_id": h["_id"]} for h in r.json()["hits"]["hits"]]


def es_chunks_by_ids(tenant_id: str, chunk_ids: list[str]) -> dict[str, dict]:
    _validate_tenant(tenant_id)
    if not chunk_ids:
        return {}
    q = {"size": len(chunk_ids), "query": {"ids": {"values": chunk_ids}}}
    r = requests.get(f"{ES_URL}/ragflow_{tenant_id}/_search", auth=ES_AUTH,
                     json=q, timeout=60)
    r.raise_for_status()
    return {h["_id"]: {**h["_source"], "_id": h["_id"]} for h in r.json()["hits"]["hits"]}


def page_texts(pdf_binary: bytes) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_binary))
    return [(p.extract_text() or "") for p in reader.pages]


def infer_mode(pages: list[str], page_index: int) -> str:
    if page_index < len(pages) and _norm(pages[page_index]):
        return "metadata"
    return "OCR"


def _norm_quote(s: str) -> str:
    """引文匹配用标准化:NFKC 统一全角/半角(如 ｜→|)后再去空白。"""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def quote_on_page(quote: str, pages: list[str], page_index: int, page_end: Optional[int] = None) -> bool:
    end = page_end if page_end is not None else page_index
    if page_index >= len(pages):
        return False
    end = min(end, len(pages) - 1)
    blob = "".join(_norm_quote(p) for p in pages[page_index:end + 1])
    return _norm_quote(quote) in blob


def _parse_chunk_header(content: str) -> dict:
    """从 chunk 首行前缀反解 scope 元数据(work 带公司+区间,project 带项目名)。

    上游 chunk 前缀格式:
      Job Responsibilities/Description(公司 2019.04-2025.08 6.3yrs): ...
      Job Responsibilities/Description(#3): [项目名] 技术栈: ...
    B0(无证据字段)与 B1 共用此反解,保证两臂年限计算口径一致。
    """
    first = (content or "").split("\n", 1)[0]
    m = re.match(r"^(?P<label>Job Responsibilities/Description|工作职责/工作描述|"
                 r"Project Description/Responsibilities|项目描述/项目职责)"
                 r"[（(](?P<w>.*)[）)]\s*:\s*(?:\[(?P<proj>[^\]]+)\])?", first)
    if not m:
        return {}
    out = {}
    label = m.group("label")
    w = (m.group("w") or "").strip()
    proj = (m.group("proj") or "").strip()
    if label.startswith("Project") or label.startswith("项目"):
        out["kind"] = "project"
        out["project_name"] = (w if w and not w.startswith(("#", "第")) else proj) or None
        return out
    if w.startswith(("#", "第")):
        # B0 合并格式:Job Responsibilities(#N): [项目名] ...
        if proj:
            out["kind"] = "project"
            out["project_name"] = proj
        return out
    dm = re.search(r"(\d{4}\.\d{1,2})\s*-\s*(\d{4}\.\d{1,2}|Present|至今|now)?", w, re.I)
    org = w[: dm.start()].strip() if dm else w
    org = re.sub(r"\s*[\d.]+yrs?$", "", org).strip()
    out["kind"] = "work"
    out["organization"] = org or None
    if dm:
        out["start_date"] = dm.group(1)
        out["end_date"] = dm.group(2) or "至今"
    return out


def chunks_to_evidence(chunks: list[dict], version: ResumeVersion,
                       pages: Optional[list[str]] = None) -> tuple[dict[str, SourceSpan], dict[str, ExperienceScope]]:
    """从 chunk 列表重建 span/scope。span id 取 chunk 的 resume_source_ref_id_kwd。"""
    spans: dict[str, SourceSpan] = {}
    scopes: dict[str, ExperienceScope] = {}
    for idx, ck in enumerate(chunks):
        content = ck.get("content_with_weight") or ck.get("content") or ""
        hdr = _parse_chunk_header(content)
        scope_id = ck.get("resume_scope_id_kwd")
        if not scope_id:
            # B0 风格 chunk(无证据字段):按前缀合成伪 scope,供年限计算与对照
            if hdr.get("kind") in ("work", "project"):
                scope_id = f"syn{idx}"
                ck = {**ck, "resume_scope_id_kwd": scope_id,
                      "resume_scope_kind_kwd": hdr["kind"]}
            else:
                continue
        if scope_id not in scopes:
            raw_kind = ck.get("resume_scope_kind_kwd") or hdr.get("kind") or "profile"
            try:
                scope_kind = ScopeKind(raw_kind)
            except ValueError:
                scope_kind = ScopeKind.PROFILE
            scopes[scope_id] = ExperienceScope(
                scope_id=scope_id,
                kind=scope_kind)
        sc = scopes[scope_id]
        if hdr.get("kind") == "work":
            sc.organization = sc.organization or hdr.get("organization")
            sc.start_date = sc.start_date or hdr.get("start_date")
            sc.end_date = sc.end_date or hdr.get("end_date")
        elif hdr.get("kind") == "project":
            sc.project_name = sc.project_name or hdr.get("project_name")
        if ck.get("resume_span_status_kwd") != "located":
            continue
        positions = ck.get("position_int") or []
        if not positions:
            continue
        # add_positions 内部对页码 +1 存储,读回时还原为 0 起
        rects = [Rect(page=int(p[0]) - 1, x0=p[1], x1=p[2], top=p[3], bottom=p[4]) for p in positions]
        page_index = rects[0].page
        quote = ck.get("resume_span_quote_ltks", "")
        mode = infer_mode(pages, page_index) if pages is not None else "unknown"
        status = "located"
        # 文本型 PDF:引文必须能在该页文本中匹配;扫描件无文本层,以 OCR 坐标为准,
        # 不做引文对页校验,报告侧标注 OCR 来源(规格 7.1/8)。
        if pages is not None and quote and mode == "metadata":
            if not quote_on_page(quote, pages, page_index, page_end=rects[-1].page):
                status = "unlocated"
        for ref in (ck.get("resume_source_ref_id_kwd") or []):
            if ref in spans:
                continue
            spans[ref] = SourceSpan(
                source_sha256=version.source_sha256,
                page_index=page_index,
                line_start=0,
                line_end=0,
                rects=rects if status == "located" else [],
                quote=quote,
                quote_hash=hashlib.sha256(_norm(quote).encode()).hexdigest(),
                extraction_mode=mode,
                status=status,
            )
            scopes[scope_id].source_span_ids.append(ref)
    return spans, scopes


def span_precision(spans: dict[str, SourceSpan]) -> tuple[int, int]:
    """(located 数, 总数) —— 评测用:定位精确率由 quote_on_page 在构建时保证。"""
    located = sum(1 for s in spans.values() if s.status == "located")
    return located, len(spans)


# 条件句里的套话不是定位词,去掉后剩下的才能在页面上逐行比对
_STOP_REQ = re.compile(r"(熟练掌握|熟悉|掌握|精通|了解|具备|具有|至少|年以上|相关|经验|优先|"
                       r"要求|以及|能够|参与|负责|独立|完成|支持|功能|系统|平台)")


def keyword_tokens(text: str) -> list[str]:
    """条件句/引文 → 字面定位词:技术名(ASCII)优先,中文去套话后取词串。"""
    ascii_toks = re.findall(r"[A-Za-z][A-Za-z0-9+#./-]*", text or "")
    zh_toks = re.findall(r"[\u4e00-\u9fff]{2,}", _STOP_REQ.sub(" ", text or ""))
    return ascii_toks + zh_toks


# 简历通用词不能当定位词:条件写"支付或风控",命中"开发/系统/模块"的那句不是证据
_GENERIC_ZH = {"开发", "业务", "模块", "项目", "技术", "产品", "功能", "服务", "系统", "平台",
               "能力", "经验", "工作", "内容", "描述", "职责", "业绩", "成果",
               "前后端", "后端", "前端", "接口", "页面", "工程师", "公司", "团队",
               "使用", "实现", "完成", "负责", "参与", "独立", "相关", "优先", "要求",
               "以及", "能够", "支持", "至少", "以上", "多年", "熟练", "熟悉", "掌握",
               "擅长", "精通", "了解", "具备", "具有", "良好", "优秀", "扎实", "深入",
               "广泛", "较强", "一定", "基础", "实际", "落地",
               "领域", "行业", "方向", "框架", "主导", "特定", "编程", "语言", "软件"}
_GENERIC_EN = {"and", "or", "the", "of", "to", "in", "with", "for", "is", "at", "a", "an"}
_SPLIT_ZH = re.compile(r"[、，,;；/／\s]+|(?:或|和|及|与|的|等)|" +
                       "|".join(sorted(_GENERIC_ZH, key=len, reverse=True)))
_LEAD_JUNK = re.compile(r"^[有会懂能可需须]")


def requirement_keywords(text: str) -> list[str]:
    """招聘条件 → 能当证据的关键词。

    比 keyword_tokens 严:连接写法拆开("支付或风控"=两个词),简历通用词丢掉,
    三四字词整词匹配不拆二字(拆了会把"数据库"匹配到"数据报表"、"规则引擎"匹配到"工作流引擎")。
    """
    out: list[str] = []
    for tok in keyword_tokens(text):
        if tok.isascii():
            for part in [tok.lower(), *re.split(r"[/\\|+]+", tok.lower())]:
                if len(part) >= 2 and part not in _GENERIC_EN:
                    out.append(part)
            continue
        for w in _SPLIT_ZH.split(tok):
            w = _LEAD_JUNK.sub("", w)
            if len(w) < 2 or w in _GENERIC_ZH:
                continue
            out.append(w)
            if len(w) >= 5:  # 长串多半是并列写法,补二字组合兜住"支付风控"
                out += [w[i:i + 2] for i in range(0, len(w) - 1, 2)]
                if len(w) % 2:
                    out.append(w[-2:])
    return list(dict.fromkeys(out))


def page_lines(pdf_binary: bytes, page_index: int) -> list[dict]:
    """页内整行矩形 + 行文本(pdfplumber 点坐标,y 向下)。"""
    if not pdf_binary:
        return []
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_binary)) as pb:
        if page_index < 0 or page_index >= len(pb.pages):
            return []
        words = pb.pages[page_index].extract_words(use_text_flow=True, keep_blank_chars=False)
    grouped: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if grouped and abs(w["top"] - grouped[-1][0]["top"]) <= 3:
            grouped[-1].append(w)
        else:
            grouped.append([w])
    return [dict(page=page_index,
                 x0=min(x["x0"] for x in ln), x1=max(x["x1"] for x in ln),
                 top=min(x["top"] for x in ln), bottom=max(x["bottom"] for x in ln),
                 text="".join(x["text"] for x in ln))
            for ln in grouped]


def narrow_rects_to_keywords(pdf_binary: bytes, page_index: int, rects: list[dict],
                             requirement: str, max_lines: int = 2) -> list[dict]:
    """把整段 chunk 的坐标收窄到真正含条件关键词的那几行。

    RAGFlow 给 span 存的是**整段**坐标(一段经历 = 抬头 + 正文,每行一个矩形),直接画出来
    就是满屏红框,HR 看不出到底哪句算证据。收窄后一行都不含关键词时只留首行(经历抬头),
    那也正是报告左栏写的那句。
    """
    toks = requirement_keywords(requirement)
    lines = page_lines(pdf_binary, page_index)
    if not lines:
        return []
    covered: dict[float, dict] = {}
    for rc in rects:
        if rc.get("page", page_index) != page_index:
            continue
        for ln in lines:
            if (ln["top"] >= rc.get("top", 0) - 2 and ln["bottom"] <= rc.get("bottom", 1e9) + 2
                    and ln["x1"] > rc.get("x0", 0) - 2 and ln["x0"] < rc.get("x1", 1e9) + 2):
                covered[round(ln["top"], 1)] = ln
    block = [covered[k] for k in sorted(covered)]
    if not block:
        return []
    hits = [ln for ln in block
            if any(_norm_quote(t).lower() in _norm_quote(ln["text"]).lower() for t in toks)]
    return hits[:max_lines] if hits else block[:1]


def line_rects_for_quote(pdf_binary: bytes, page_index: int, needle: str,
                         max_lines: int = 4) -> list[dict]:
    """把引文定位到页内整行矩形(返回 pdfplumber 点坐标,y 向下)。

    RAGFlow 的 resume 解析只给"经历抬头行"存了 position_int,正文里真正支撑结论的那句
    (如 · 后端技术|Spring Boot、MySQL…)没有坐标,证据框只能框到抬头。
    这里服务端用 pdfplumber 现算行矩形,把框落到句子上;定位不到则返回空由调用方兜底。
    """
    toks = keyword_tokens(needle)
    if not toks or not pdf_binary:
        return []
    lines = page_lines(pdf_binary, page_index)
    if not lines:
        return []

    needle_blob = _norm_quote(needle).lower()
    threshold = 2 if len(toks) > 1 else 1
    scored = []
    for idx, ln in enumerate(lines):
        blob = _norm_quote(ln["text"]).lower()
        if not blob:
            continue
        score = sum(1 for t in toks if _norm_quote(t).lower() in blob)
        if len(blob) >= 12 and blob[:12] in needle_blob:
            score += 2  # 整行就是引文的一部分
        if score >= threshold:
            scored.append((score, idx, ln))
    if not scored:
        return []
    # 一次只框一句:同分的散落命中(姓名栏和职位行都含"架构师/Java")只留最像引文的那行;
    # 引文长到一行装不下时,才把**紧邻**的上下行并进来(技能清单那种一段摊成几行的情况)
    def explained(ln):
        t = _norm_quote(ln["text"]).lower()
        return sum(1 for ch in t if ch in needle_blob) / len(t)
    best = max(s for s, _, _ in scored)
    anchor = max([e for e in scored if e[0] == best], key=lambda e: (explained(e[2]), -e[1]))
    by_idx = {e[1]: e for e in scored}
    need_more = len(_norm_quote(needle)) > len(_norm_quote(anchor[2]["text"])) * 1.2
    sel = [anchor[1]]
    if need_more:
        for direction in (1, -1):
            i = anchor[1] + direction
            while (i in by_idx and by_idx[i][0] >= threshold
                   and len(sel) < max_lines):
                sel.append(i)
                i += direction
    return [dict(by_idx[k][2]) for k in sorted(set(sel))]

