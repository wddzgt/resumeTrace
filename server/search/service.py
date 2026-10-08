"""搜索服务:条件解析 → 召回 → 判断 → 评分 → 报告(e2e runner 与 API 共用)。"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Callable, Optional

import requests

from ..core.contracts import (CriterionCategory, ResumeVersion, ScopeRule,
                              validate_invariants)
from ..core.evidence import (chunks_to_evidence, es_chunks_for_document, page_texts)
from ..jd.parser import parse_conditions
from ..report.analysis import generate_candidate_analysis
from ..report.render import render_report, score_report
from ..search.orchestrator import judge_criterion
from ..search.pipeline import criterion_queries

logger = logging.getLogger("resumetrace.search")

BASE = __import__("os").environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")


def _headers(key: str):
    return {"Authorization": f"Bearer {key}"}


def run_search(dataset_id: str, query_text: str, tenant_id: str, api_key: str,
               llm_call: Callable[[str, str], str],
               scope_constraints: bool = True, top_n: int = 20,
               chat_model: str = "", criteria=None,
               on_candidate: Optional[Callable[[dict], None]] = None,
               on_stage: Optional[Callable[[str, dict], None]] = None) -> dict:
    """返回 {query_sha, criteria, warnings, candidates:[{report, summary}], spans_count}。"""
    if criteria is None:
        criteria, qsha, warns = parse_conditions(query_text, llm_call)
        logger.info("JD parsed: %d criteria, %d warnings", len(criteria), len(warns))
    else:
        qsha = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        warns = []
    for i, c in enumerate(criteria):
        c.query_id = f"q-{qsha[:8]}"
    if on_stage:
        on_stage("criteria", {"n_criteria": len(criteria)})

    pool: dict[str, list[str]] = {}
    gate = [c for c in criteria
            if c.category in (CriterionCategory.REQUIRED, CriterionCategory.SEARCH_INTENT)]
    for c in (gate or criteria):
        for q in criterion_queries(c):
            try:
                r = requests.post(f"{BASE}/api/v1/retrieval",
                                  headers={**_headers(api_key), "Content-Type": "application/json"},
                                  json=dict(question=q, dataset_ids=[dataset_id], top_k=20,
                                            similarity_threshold=0.1, keyword=True), timeout=120)
                r.raise_for_status()
                for hit in r.json().get("data", {}).get("chunks", []):
                    pool.setdefault(hit["document_id"], []).append(hit.get("id") or hit.get("chunk_id"))
            except Exception as e:
                logger.warning("Retrieval failed for query %r: %s", q[:60], e)

    try:
        docs_resp = requests.get(f"{BASE}/api/v1/datasets/{dataset_id}/documents?page=1&page_size=500",
                            headers=_headers(api_key), timeout=60)
        docs_resp.raise_for_status()
        docs = docs_resp.json()["data"]["docs"]
    except Exception as e:
        logger.error("Failed to list documents: %s", e)
        docs = []
    doc_by_id = {d["id"]: d for d in docs}
    candidates = sorted(pool, key=lambda d: -len(pool[d]))[:top_n]
    if on_stage:
        on_stage("recall", {"total": len(candidates), "pool_size": len(pool)})
    logger.info("Recall done: pool=%d candidates=%d", len(pool), len(candidates))
    as_of = datetime.date.today()

    out = []
    for idx, doc_id in enumerate(candidates):
        d = doc_by_id.get(doc_id)
        if not d:
            logger.warning("Document %s not found in listing, skipping", doc_id)
            continue
        tag = d["name"][:4]
        logger.info("Processing candidate %d/%d: %s (%s)", idx + 1, len(candidates), tag, d["name"])
        chs = es_chunks_for_document(tenant_id, doc_id)
        try:
            pdf_resp = requests.get(f"{BASE}/api/v1/datasets/{dataset_id}/documents/{doc_id}",
                               headers=_headers(api_key), timeout=120)
            pdf_resp.raise_for_status()
            pdf = pdf_resp.content
        except Exception as e:
            logger.error("PDF download failed for %s: %s", doc_id, e)
            continue
        pages = page_texts(pdf)
        ver = ResumeVersion(candidate_id=tag, dataset_id=dataset_id, document_id=doc_id,
                            source_sha256=hashlib.sha256(pdf).hexdigest(),
                            filename=d["name"], ingested_at=d.get("create_date", ""),
                            parser_version="v0.27.2+0002", status="DONE")
        spans, scopes = chunks_to_evidence(chs, ver, pages)
        scopes_by_id = {s.scope_id: s for s in scopes.values()}
        recalled = set(pool[doc_id])
        results, facts = [], []
        for c in criteria:
            cc = c if scope_constraints else c.model_copy(
                update={"scope_rule": ScopeRule.CANDIDATE_ANYWHERE})
            if scope_constraints:
                # 判断看该候选人全部经历级 chunk(按词面重叠排 top2 送模型),
                # 召回命中只决定候选池 membership,不限制证据面
                consider = [ck for ck in chs if ck.get("resume_scope_id_kwd")]
            else:
                # B0 风格:无 scope 元数据,判断器看全部 chunk(含合并描述)
                consider = chs
            r, fs = judge_criterion(cc, tag, consider, scopes_by_id, llm_call, as_of,
                                    model_version=chat_model,
                                    scope_constraints=scope_constraints,
                                    jd_text=query_text)
            results.append(r)
            facts.extend(fs)
        validate_invariants([ver], spans, facts, results)
        score = score_report(criteria, results)
        report = render_report(query_text, qsha, criteria, results, ver, spans, score,
                               facts=facts, scopes=scopes)
        analysis = generate_candidate_analysis(
            query_text=query_text,
            criteria=[c.model_dump() for c in criteria],
            results=[r.model_dump() for r in results],
            score=score,
            resume_chunks=chs,
            llm_call=llm_call,
        )
        report["analysis"] = analysis
        unaudited = [r.criterion_id for r in results
                     if r.state.value == "SUPPORTED"
                     and not any(spans.get(s) and spans[s].status == "located"
                                 for s in r.source_span_ids)]
        logger.info("Candidate %s done: score=%.1f coverage=%.1f%% unaudited=%d",
                   tag, score["score"], score["coverage_pct"], len(unaudited))
        out.append(dict(candidate=tag, document_id=doc_id, report=report,
                        score=score["score"], coverage=score["coverage_pct"],
                        unaudited_supported=unaudited,
                        states={r.criterion_id: r.state.value for r in results}))
        if on_candidate:
            on_candidate(out[-1])
    return dict(query_sha=qsha, criteria=[c.model_dump() for c in criteria],
                warnings=warns, pool_size=len(pool), candidates=out)
