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


def es_chunks_for_document(tenant_id: str, document_id: str, size: int = 300) -> list[dict]:
    q = {"size": size, "query": {"term": {"doc_id": document_id}}}
    r = requests.get(f"{ES_URL}/ragflow_{tenant_id}/_search", auth=ES_AUTH,
                     json=q, timeout=60)
    r.raise_for_status()
    return [{**h["_source"], "_id": h["_id"]} for h in r.json()["hits"]["hits"]]


def es_chunks_by_ids(tenant_id: str, chunk_ids: list[str]) -> dict[str, dict]:
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


def quote_on_page(quote: str, pages: list[str], page_index: int, page_end: Optional[int] = None) -> bool:
    end = page_end if page_end is not None else page_index
    if page_index >= len(pages):
        return False
    end = min(end, len(pages) - 1)
    blob = "".join(_norm(p) for p in pages[page_index:end + 1])
    return _norm(quote) in blob


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
            scopes[scope_id] = ExperienceScope(
                scope_id=scope_id,
                kind=ScopeKind(ck.get("resume_scope_kind_kwd") or hdr.get("kind") or "profile"))
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
