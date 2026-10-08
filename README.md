<div align="center">

# ResumeTrace

**基于证据的简历筛选系统 — 每个判断都可溯源到 PDF 原文**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![RAGFlow v0.27.2](https://img.shields.io/badge/based%20on-RAGFlow%20v0.27.2-009688?style=flat-square)](https://github.com/infiniflow/ragflow)

</div>

---

## 项目简介

解决 LLM 简历筛选中的**幻觉问题**：传统方案给一个分数但无法验证依据，ResumeTrace 要求每个判断都附带原文引用和 PDF 坐标，点击即可跳转到简历原文对应位置。

核心机制：JD 解析 → 逐条证据提取 → LLM 判定（附逐字引用）→ 引用审计（验证引用是否真实出现在原文）→ 确定性评分 → 生成报告。

**适用场景：** AI 招聘 / 智能简历筛选 / ATS 增强 / HR Tech / 候选人匹配 / 面试辅助 / 人才搜索 / 简历解析 / JD 匹配 / 证据溯源

**技术标签：** `LLM` `RAG` `Retrieval-Augmented Generation` `AI Agent` `Hallucination Prevention` `Evidence-Based Reasoning` `Document AI` `PDF Understanding` `Vector Search` `Embedding` `NLP` `FastAPI` `RAGFlow`

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | Python 3.10+ / FastAPI / Uvicorn |
| RAG 引擎 | RAGFlow v0.27.2（fork，增加 evidence parser 补丁） |
| LLM | DashScope（deepseek-v4.1-flash）/ 任意 OpenAI 兼容接口 |
| 向量检索 | RAGFlow 内置（关键词 + 语义混合召回） |
| PDF 渲染 | pypdfium2 + Pillow |
| 存储 | SQLite（报告/搜索状态） |
| 部署 | Docker Compose（RAGFlow）+ 本地 Python 进程（API） |

## 快速开始

### 依赖

- Docker & Docker Compose
- Python 3.10+
- DashScope API Key（或其他 OpenAI 兼容 LLM）

### 一键启动

```bash
export RAGFLOW_API_KEY=your_key   # 首次需设置
./start.sh                        # 前台启动(Ctrl+C 停止)
# 或
make start                        # 等价
```

`start.sh` 自动完成：启动 RAGFlow 容器 → 等待就绪 → 注入证据模式补丁 → 安装依赖 → 启动 API。

更多命令：

```bash
make bg          # 后台启动(日志写入 .resumetrace.log)
make stop        # 停止所有服务
make status      # 查看运行状态
make logs        # 查看后台 API 日志
```

### 首次配置

启动后需在 RAGFlow UI (http://localhost:9380) 配置模型：
- Chat: `deepseek-v4.1-flash`
- Embedding: `text-embedding-v4`（batch ≤ 10）

然后打开 http://localhost:8000/ui/ 即可使用。

## 架构流程

```
JD 文本 ──► JD 解析器 ──► JobCriterion[]（结构化条件 + 偏移校验）
                                │
简历 PDF ──► 证据解析器 ──► Chunks（带经历范围元数据）
                                │
                    逐条证据判定（每个条件 × 每个候选人）
                    ├─ 召回相关 chunks（关键词 + 语义）
                    ├─ LLM 判定: SUPPORTED / PARTIAL / NOT_EVIDENCED
                    ├─ 引用审计: 引用必须逐字出现在原文 chunk 中
                    └─ 跨经历拼接检测: AND 条件必须来自同一经历范围
                                │
                    确定性评分
                    Score = Σ(weight × state) / Σ(weight)
                    SUPPORTED=1.0, PARTIAL=0.5, 其他=0.0
                                │
                    报告生成 + LLM 分析
                    （优势 / 风险 / 面试建议问题）
```

### 核心模块

| 路径 | 职责 |
|------|------|
| `server/jd/parser.py` | JD → 结构化条件，偏移校验 |
| `server/search/orchestrator.py` | 证据判定，8 层幻觉防御 |
| `server/search/pipeline.py` | 评分逻辑（AND/OR、范围规则、年限计算） |
| `server/report/render.py` | 结构化报告 + Markdown 导出 |
| `server/report/analysis.py` | LLM 分析（优势 / 风险 / 面试问题） |
| `server/core/contracts.py` | 数据模型 + 不变量校验 |
| `server/core/evidence.py` | Chunk → 证据 span 映射 |
| `ragflow-fork/` | RAGFlow v0.27.2 fork，含证据解析器补丁 |

### RAGFlow 增强架构

ResumeTrace 不直接修改 RAGFlow 源码，而是通过 **补丁 + 伴生 API** 三层增强，使 RAGFlow 的简历解析器输出可溯源的证据结构：

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Parser Patches (ragflow-fork/patches/)        │
│                                                         │
│  RAGFlow 原始简历解析器只输出 chunk 文本，无证据坐标。   │
│  4 个补丁注入证据模式，使每个 chunk 携带：              │
│   • resume_scope_id / kind   — 经历范围（哪段工作/项目）│
│   • resume_span_status       — 定位状态（located/...）  │
│   • resume_span_quote        — 原文引用文本              │
│   • position_int             — 真实 PDF 矩形坐标         │
│   • resume_source_ref_id     — 证据 span 唯一标识        │
│                                                         │
│  补丁通过 deploy/apply_patches.sh 注入容器，            │
│  容器重建后需重跑。                                      │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Evidence Reading (server/core/evidence.py)    │
│                                                         │
│  RAGFlow 的 chunk/检索 API 会裁掉自定义字段，           │
│  因此证据读取直连 Elasticsearch：                        │
│   • es_chunks_for_document() — 按文档拉全量 chunks      │
│   • es_chunks_by_ids()       — 按 chunk ID 批量取       │
│   • chunks_to_evidence()     — 重建 SourceSpan +        │
│                                ExperienceScope 契约对象  │
│   • quote_on_page()          — 引用审计（引文必须        │
│                                逐字匹配 PDF 页文本）     │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Companion API (server/api/)                   │
│                                                         │
│  FastAPI 伴生服务，编排 JD 解析 → 证据判定 → 评分 →    │
│  报告生成，并通过 RAGFlow API 完成检索与 LLM 调用。     │
└─────────────────────────────────────────────────────────┘
```

**补丁清单：**

| 补丁 | 修复/增强 |
|------|-----------|
| `0000-baseline-repair-llmbundle.patch` | 修复 v0.27.2 上游 bug：`LLMBundle` 调用传参错误导致简历解析静默降级为仅基本信息 |
| `0001-embedding-batch-configurable.patch` | Embedding 批大小可配置（`RAGFLOW_EMBED_BATCH_SIZE`），DashScope 兼容模式上限 ≤10 |
| `0002-evidence-mode.patch` | 核心增强：注入证据模式，chunk 携带经历范围、引用文本、PDF 矩形坐标 |
| `0003-kimi-k3-temperature.patch` | DashScope 托管的 kimi-k3 拒绝显式 temperature，补丁自动移除 |

**证据模式（Evidence Mode）工作原理：**

传统 RAGFlow 简历解析将 PDF 切成文本 chunk，丢失了"这段话出自哪段工作经历"和"在 PDF 第几页第几行"的信息。证据模式在解析阶段为每个 chunk 注入结构化元数据：

1. **经历范围（ExperienceScope）**：识别 chunk 属于哪段工作经历或项目经历，提取公司名、时间段、项目名
2. **证据 Span（SourceSpan）**：每个 chunk 的引用文本 + 在 PDF 中的真实矩形坐标（由 pypdfium2 文本层提取，非合成坐标）
3. **引用审计**：证据读取时验证引用文本是否逐字出现在对应 PDF 页，不匹配则标记为 `unlocated`，不作为"明确支持"的证据
4. **证据框落到句子**：解析器给 span 存的是**整段**坐标（一段经历 = 抬头 + 正文，每行一个矩形），全画出来就是满屏红框。渲染时两级收窄：
   - 模型有逐字摘抄句（`evidence_quote`）→ 用 pdfplumber 现算那一行的矩形，框只盖住那句；
   - 没有摘抄句（如年限类条件只用到抬头）→ 只框整段里**含条件关键词的那一行**，一行都不含时框经历抬头行。
   一次只框一句：既不盖住整段，也不跨页乱框 —— `server/core/evidence.py` 的 `line_rects_for_quote()` / `narrow_rects_to_keywords()`。
5. **引用与条件对齐** — 补历史报告的引用时（`scripts/backfill_partial_citations.py`）只认条件里的技术名和领域词（`Spring Boot`、`支付`、`风控`），`开发`/`系统`/`模块` 这类简历通用词不算命中；一个 chunk 拆出的两个同坐标 span 会去重，一排"看原文"按钮不再指向同一处；左栏"简历:"那句也随右栏正在显示的引证切换，避免"要求 / 引文 / 红框"三处各说各话

这使得下游的 LLM 判定结果可以追溯到 PDF 原文的具体位置，实现"每个判断都可溯源"。

## API 示例

### 创建搜索

```bash
curl -X POST http://localhost:8000/v1/searches \
  -H "X-RT-Token: local-dev" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": "<dataset_id>",
    "query_text": "5年以上Flink流批处理经验，熟悉Kafka",
    "limit": 10
  }'
```

### 返回结构

```json
{
  "search_id": "abc123",
  "query_sha": "sha256...",
  "criteria": [
    {
      "criterion_id": "c01",
      "category": "required",
      "expression": {"predicate": "流批处理技术", "value": "Flink"},
      "min_duration_months": 60
    }
  ],
  "candidates": [
    {
      "candidate": "CV-01",
      "score": 75.0,
      "coverage": 80.0,
      "states": {"c01": "SUPPORTED", "c02": "PARTIAL"}
    }
  ]
}
```

### 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/libraries/import` | 导入简历数据集 |
| POST | `/v1/searches` | 创建搜索（JD → 条件 → 逐条判定） |
| GET  | `/v1/searches/{id}` | 获取搜索结果 |
| GET  | `/v1/reports/{id}` | 获取候选人报告 |
| GET  | `/v1/evidence/{span}/png` | PDF 原文页（高亮证据） |
| GET  | `/v1/resume/page/png` | 简历原文页（无高亮） |
| GET  | `/health` | 健康检查 |

### 错误码

| 码 | 含义 |
|----|------|
| `ARG` | 参数错误 |
| `NO_PDF` | 找不到 PDF 文件 |
| `AUTH` | 认证失败（X-RT-Token 无效） |

## 幻觉防御

9 层程序化防御，不依赖 LLM 可信度：

1. **引用审计** — LLM 提供的引用必须逐字出现在原文 chunk 中，否则降为 `NEEDS_REVIEW`
2. **跨经历拼接检测** — AND 条件必须来自同一工作/项目经历；判为 `PARTIAL` 时**仍保留已定位到的原文引用**，HR 能看到"哪条要求缺证据"而不是"未找到可定位原文"
3. **原文定位验证** — 每处证据必须映射到有效 PDF 页码 + 坐标
4. **不变量校验** — `SUPPORTED` 必须引用有效 span
5. **分数-匹配度对齐** — score=0 时 fit_level 强制为"弱匹配"/"不匹配"
6. **概要/技能降级** — 技能清单区的声称不算证据，除非有工作经历支撑
7. **确定性评分** — 评分无 LLM 参与，SUPPORTED=1.0, PARTIAL=0.5
8. **领域判断放宽** — 年限计算严格，领域判定（哪些经历算数）宽松
9. **岗位原文展示不截断** — 条件解析模型给的字符偏移常切在词中间（`MySQL,有支付…` 被切成 `ySQL,有支付或风控系统开`），展示前把边界推回小句/编号条目边界还原成完整一句（`server/jd/parser.py` 的 `snap_fragment()`）；"待确认"话术同样点名这句岗位原文，不吐内部条件标签，避免"要求 / 引文 / 结论"三处各说各话

详见 [docs/HALLUCINATION_DEFENSE.md](docs/HALLUCINATION_DEFENSE.md)

## 测试与部署

### 测试

```bash
pytest tests/ -v                                         # 单元测试
python3 eval/runner/it_p1_evidence.py --dataset-id <id>  # 证据提取精度
python3 eval/runner/test_jd_regression.py                # JD 解析回归
python3 eval/runner/e2e_search_report.py --dataset-id <id>  # 端到端
```

### 部署

```bash
# 开发环境
./start.sh --bg          # 后台启动

# 生产环境(建议 systemd 或 supervisor 管理 API 进程)
./start.sh --bg          # RAGFlow + 补丁 + API
# 或分步:
cd deploy && docker compose --profile cpu up -d
bash ../deploy/apply_patches.sh
python3 -m uvicorn server.api.main:app --host 0.0.0.0 --port 8000 --workers 2
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAGFLOW_API_KEY` | （必填） | RAGFlow API 密钥 |
| `RAGFLOW_BASE_URL` | `http://localhost:9380` | RAGFlow 服务地址 |
| `RAGFLOW_CHAT_MODEL` | `deepseek-v4.1-flash` | Chat 模型 |
| `RAGFLOW_EMBED_BATCH_SIZE` | `10` | Embedding 批大小（DashScope ≤ 10） |
| `RESUMETRACE_TOKEN` | `local-dev` | API 认证 token |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 贡献指南

1. Fork 仓库，创建功能分支
2. 为新功能编写测试
3. 代码风格：`black` 格式化，`ruff` lint
4. 提交 PR，附清晰描述

当前开放方向：

- React/TypeScript UI 重构（当前为单页演示）
- 英文简历/JD 支持
- 真实评测数据集（200+ 份简历）
- ATS 系统集成（Greenhouse、Lever、Workday）

## 关键词 / Topics

`Resume Screening` `AI Recruitment` `HR Tech` `ATS` `Applicant Tracking System` `Talent Acquisition` `Candidate Matching` `Job Description Parsing` `Resume Parsing` `CV Analysis` `LLM Application` `RAG` `Retrieval-Augmented Generation` `AI Agent` `Hallucination Prevention` `Evidence-Based AI` `Explainable AI` `XAI` `Document Understanding` `PDF Extraction` `OCR` `Vector Search` `Semantic Search` `Embedding` `NLP` `Natural Language Processing` `FastAPI` `Python` `RAGFlow` `Open Source`

## 许可证

MIT. 详见 [LICENSE](LICENSE).

---

<div align="center">

**联系 / Contact:** 460856760@qq.com

</div>
