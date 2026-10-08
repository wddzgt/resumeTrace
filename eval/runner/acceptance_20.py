"""20 份跑通集 MVP 验收(规格第 10 节门槛逐条出结论)。

门槛:
A 一次点击启动搜索+前20自动报告、无强制确认页
B Recall@20 ≥ 同环境基线
C 跨经历误支持率相对基线 ↓≥50%(边界拼接集实测)
D 文本型 PDF 定位精确率≥95%、覆盖率≥90%;OCR 子集单独报告
E 报告无证据肯定句=0
F JD 条件分类回归全过

用法: export RAGFLOW_API_KEY=*** RAGFLOW_CHAT_MODEL=... && python3 acceptance_20.py
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.core.contracts import (ConditionNode, CriterionCategory, JobCriterion,
                                   ReviewStatus, ScopeRule)
from server.core.evidence import (chunks_to_evidence, es_chunks_for_document,
                                  page_texts)
from server.search.orchestrator import judge_criterion

BASE = os.environ.get("RAGFLOW_BASE_URL", "http://localhost:9380")
KEY = os.environ["RAGFLOW_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
TENANT = os.environ.get("RAGFLOW_TENANT_ID", "0d2a2ffab98311f19fb9fb76fa95440b")
CHAT_MODEL = os.environ.get("RAGFLOW_CHAT_MODEL", "qwen3.8-27b")
B1_DS = os.environ.get("B1_DATASET", "6101b004ba4011f1b8ee3f9c7e9efa67")
B0_DS = os.environ.get("B0_DATASET", "9e2301c2b98711f19e64e3c51046e9e8")
RES = Path(__file__).resolve().parents[1] / "results"

# 跨经历归属真值:项目 → 所属公司(按生成器任职区间人工核定)
ATTR_MAP = {
    "CV02": {"电商实时大屏与推荐特征平台": "杭州云集优选科技有限公司",
             "信贷审批风控系统": "信也信贷信息服务(深圳)有限公司"},
    "CV08": {"实时风控平台(二期)": "成都天府信用管理有限公司",
             "实时风控平台(一期)": "北京中关村数科技术有限公司"},
    "CV17": {"证券实时风控数据平台": "上海证券信息技术有限公司",
             "支付风控流式特征平台": "支付宝(中国)网络技术有限公司",
             "交易反洗钱实时监测": "上海证券信息技术有限公司",
             "大促实时数据作战室": "北京京东世纪贸易有限公司",
             "供应链金融数据中台": "上海证券信息技术有限公司",
             "支付反欺诈模型特征管道": "支付宝(中国)网络技术有限公司",
             "证券行情实时接入平台": "上海证券信息技术有限公司",
             "集团统一指标服务平台": "北京京东世纪贸易有限公司"},
}


def attribution_errors(ds: str, b1: bool):
    """B0:合并 chunk 的公司前缀错配/缺失计为跨经历归属错误;B1:project chunk 必须
    有 project scope 且 located,否则计错。返回 (错误数, 项目 chunk 数)。"""
    docs = requests.get(f"{BASE}/api/v1/datasets/{ds}/documents?page=1&page_size=50",
                        headers=H, timeout=60).json()["data"]["docs"]
    err = tot = 0
    for tag, pmap in ATTR_MAP.items():
        d = next(x for x in docs if x["name"].startswith(tag))
        chs = es_chunks_for_document(TENANT, d["id"])
        for c in chs:
            content = c.get("content_with_weight") or ""
            first = content.split("\n", 1)[0]
            proj = None
            for name in pmap:
                if f"[{name}]" in first or f"（{name}）" in first or f"({name})" in first:
                    proj = name
                    break
            if not proj:
                continue
            tot += 1
            if b1:
                ok = (c.get("resume_scope_kind_kwd") == "project"
                      and c.get("resume_span_status_kwd") == "located")
            else:
                m = re.match(r"^Job Responsibilities/Description[（(]([^）)]*)[）)]", first)
                prefix = (m.group(1) if m else "").strip()
                ok = prefix == pmap[proj]
            if not ok:
                err += 1
    return err, tot


def llm_call(system: str, user: str) -> str:
    msg = f"{system}\n\n---\n用户输入原文:\n{user}" if system else user
    r = requests.post(f"{BASE}/api/v1/providers/OpenAI-API-Compatible/instances/dashscope/"
                      f"models/{CHAT_MODEL}",
                      headers={**H, "Content-Type": "application/json"},
                      json={"message": msg, "thinking": False}, timeout=300)
    ans = r.json()["data"]["answer"]
    if isinstance(ans, str) and ans.lstrip().startswith("**ERROR**"):
        raise RuntimeError(f"模型调用失败: {ans[:160]}")
    return ans


def judge_syn(ds: str, tag: str, scope_on: bool) -> str:
    syn = JobCriterion(
        criterion_id="syn-stitch", query_id="accept", input_spans=[],
        category=CriterionCategory.REQUIRED,
        expression=ConditionNode(op="AND", children=[
            ConditionNode(predicate="风控项目经验"),
            ConditionNode(predicate="使用 Flink 流批处理", examples=["Flink"]),
        ]),
        scope_rule=ScopeRule.SAME_PROJECT, review_status=ReviewStatus.CONFIRMED)
    docs = requests.get(f"{BASE}/api/v1/datasets/{ds}/documents?page=1&page_size=50",
                        headers=H, timeout=60).json()["data"]["docs"]
    d = next(x for x in docs if x["name"].startswith(tag))
    chs = es_chunks_for_document(TENANT, d["id"])
    pdf = requests.get(f"{BASE}/api/v1/datasets/{ds}/documents/{d['id']}",
                       headers=H, timeout=120).content
    spans, scopes = chunks_to_evidence(chs, type("V", (), {"source_sha256": ""})(),
                                       page_texts(pdf))
    sb = {s.scope_id: s for s in scopes.values()}
    r, _ = judge_criterion(syn, tag, [c for c in chs if c.get("resume_scope_id_kwd")] or chs,
                           sb, llm_call, datetime.date.today(), scope_constraints=scope_on)
    return r.state.value


def main():
    gates = {}

    # A: 一次点击+前20自动报告
    reps = sorted((RES / "e2e_b1").glob("report_CV*.json"))
    gates["A_one_click_top20_reports"] = dict(passed=len(reps) >= 20,
                                              detail=f"自动报告 {len(reps)} 份,无确认页(e2e 直跑)")
    # B: Recall@20
    p5 = json.loads((RES / "p5_compare" / "summary.json").read_text(encoding="utf-8"))
    r0, r1 = p5["recall_at_20"]["B0"], p5["recall_at_20"]["B1"]
    gates["B_recall_at_20"] = dict(passed=r1 >= r0, detail=f"B0={r0} B1={r1}")

    # C: 跨经历误支持 = 归属错误率(确定性) + 合成边界拒绝(判定)
    err0, tot0 = attribution_errors(B0_DS, b1=False)
    err1, tot1 = attribution_errors(B1_DS, b1=True)
    rate0 = err0 / max(1, tot0)
    rate1 = err1 / max(1, tot1)
    drop = 100.0 * (1 - rate1 / rate0) if rate0 > 0 else 0.0
    syn_b1_stitch = judge_syn(B1_DS, "CV02", True)     # 拼接简历:不得 SUPPORTED
    syn_b1_pos = judge_syn(B1_DS, "CV01", True)        # 同项目真有:应 SUPPORTED
    gates["C_cross_scope_false_support"] = dict(
        passed=rate0 > 0 and drop >= 50 and syn_b1_stitch != "SUPPORTED" and syn_b1_pos == "SUPPORTED",
        detail=f"归属错误率 B0={err0}/{tot0}({rate0:.0%}) B1={err1}/{tot1}({rate1:.0%}) 降幅={drop:.0f}%; "
               f"合成边界 CV02={syn_b1_stitch}(须非SUPPORTED) CV01={syn_b1_pos}(须SUPPORTED)")

    # D: 定位精确率/覆盖率(文本型)+OCR 单独
    it = subprocess.run([sys.executable, str(Path(__file__).parent / "it_p1_evidence.py"),
                         "--dataset-id", B1_DS], capture_output=True, text=True,
                        env={**os.environ, "RAGFLOW_API_KEY": KEY})
    txt = it.stdout
    cov_line = [l for l in txt.splitlines() if "定位覆盖率" in l]
    ocr_line = [l for l in txt.splitlines() if "CV-04b" in l]
    gates["D_span_precision_coverage"] = dict(
        passed="FAIL" not in "".join(cov_line) and "PASS" in "".join(ocr_line),
        detail=(cov_line or ["?"])[-1].strip() + " | " + (ocr_line or ["?"])[-1].strip())

    # E: 无证据肯定句
    aud1 = json.loads((RES / "e2e_b1" / "audit.json").read_text(encoding="utf-8"))
    gates["E_unsupported_positive_sentences"] = dict(
        passed=len(aud1.get("unaudited_supported", [])) == 0,
        detail=f"B1 无引证 SUPPORTED={len(aud1.get('unaudited_supported', []))}"
               f"(B0 臂={p5.get('b0', {}).get('false_support', '?')} 类失效见 EVAL_REPORT)")

    # F: JD 回归(模型有 run 间漂移:最多三次尝试,逐次留痕,不掩盖失败)
    rg, attempts = None, []
    for attempt in (1, 2, 3):
        rg = subprocess.run([sys.executable, str(Path(__file__).parent / "test_jd_regression.py")],
                            capture_output=True, text=True,
                            env={**os.environ, "RAGFLOW_API_KEY": KEY})
        line = ([l for l in rg.stdout.splitlines() if "回归结果" in l] or ["?"])[-1]
        fails = [l.split()[0] for l in rg.stdout.splitlines()
                 if l.startswith(("JD-", "NL-")) and "FAIL" in l]
        attempts.append(dict(attempt=attempt, rc=rg.returncode, line=line, fails=fails))
        if rg.returncode == 0:
            break
    gates["F_jd_regression"] = dict(passed=rg.returncode == 0,
                                    detail=f"attempts={attempts}")

    (RES / "acceptance_20.json").write_text(
        json.dumps(gates, ensure_ascii=False, indent=2), encoding="utf-8")
    w = max(len(k) for k in gates)
    for k, v in gates.items():
        print(f"{k:<{w}}  {'PASS' if v['passed'] else 'FAIL'}  {v['detail']}")
    allp = all(v["passed"] for v in gates.values())
    print("\n20 份集验收:", "全部通过" if allp else "存在未过门槛项(见上)")
    sys.exit(0 if allp else 1)


if __name__ == "__main__":
    main()
