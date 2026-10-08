# B0 基线报告(未修改/仅修复 RAGFlow v0.27.2)

日期:2026-09-26|环境:macOS arm64,Docker 12GB,ragflow v0.27.2(amd64/Rosetta),
chat=deepseek-v4.1-flash、embedding=text-embedding-v4(DashScope compatible-mode),ES 8.11.3 MEM_LIMIT 4GB。
数据集:`resumetrace-b0`(损坏版测量,dataset a30b0514…)与 `resumetrace-b0r`(修复版有效基线,dataset 9e2301c2…)。
简历:20 份合成 PDF(~/Desktop/简历库,resumes+ground_truth),解析配置 chunk_method=resume。

## 1. 两组测量

| 指标 | b0(原版代码) | b0r(0000+0001 修复后) |
|---|---|---|
| 解析成功率 | 20/20 DONE(假成功) | 20/20 DONE(CV17 需 0001 补丁) |
| 每文档 chunk 数 | **1**(仅 Basic Info) | 7–18(字段级) |
| 经历/项目字段 | 全部丢失 | 有(work/project 分 chunk) |
| 解析耗时 | 56s(无 LLM 抽取) | 489s(真实 LLM 抽取) |
| q_full_jd 命中(top_k20,thr0.1) | 11(全为基本信息 chunk) | 30(经历级 chunk) |
| 检索延迟 p95 | 5.05s | 9.6s |

损坏原因见 `docs/UPSTREAM_BUGS.md` BUG-001(LLMBundle 传参类型)与
BUG-002(OpenAIEmbed batch=16 > DashScope 上限 10,多 chunk 文档 embedding 400)。
修复补丁:`ragflow-fork/patches/0000-baseline-repair-llmbundle.patch`、
`0001-embedding-batch-configurable.patch`(后者经 RAGFLOW_EMBED_BATCH_SIZE=10 启用)。
**评测对照以 b0r 为 B0**;b0 保留为"上游缺陷"证据。

## 2. 修复版 chunk 结构实测(CV02 样例)

chunk 类型:Basic Info / Skills & Certificates / Work Overview /
Project Experience(项目名合并列)/ Job Responsibilities(按 work 与按 project #N 分)/ Self-Evaluation。

已确认的三个证据缺口(P1 二开目标):
1. **原文位置缺失**:`positions` 为模拟坐标 `[[page,0,0,i,i]]`,与 PDF 真实行/矩形无关。
2. **项目归属缺失**:project #N chunk 不含公司/任职区间;Project Experience chunk 把多个项目名合并进一个 chunk;work 与 project 的父子关系无处表达(后处理还会把 project_desc 并入 work_desc)。
3. **概要chunk 越权风险**:Self-Evaluation/Summary chunk 携带"X 年 Y 方向"式自述,
   q_full_jd 下 CV13(方向年限仅 1.8 年)凭 Summary 排到第 2——判断器若直接采信概要即误支持。

## 3. 对抗性观察(b0r 检索,供 P3 判断层设计)

- **CV-01 跨经历拼接**:q_stream("Flink 实时风控平台…")第 10 命中是 CV02 的*电商* Flink 项目 chunk
  (sim 0.319),同文档另有风控 work chunk;无 scope 约束的判断器会把两者拼成"风控项目用 Flink"。
- **JD-01 OR 语义**:q_lang_or 下 CV10(仅 C++)高位命中(sim 0.299),召回层行为正确,否决风险在判断层。
- **CV-02 清单 vs 经历**:CV03 的 Skills chunk 含 Flink、项目 chunk 仅 Spark;召回会同时命中两者,
  判断层必须按 chunk 归属区分"清单声称"与"经历证明"。
- **CV20 模糊简历**:修复后不再凭基本信息 chunk 高位混入(q_lead top10 已无 CV20),
  但其"深度参与/熟悉各类"自述 chunk 仍可能被语义判断采信→需要"无具体经历支撑不得 SUPPORTED"规则。
- **OCR**:CV10(扫描版)解析与检索正常(chunk 9 个),OCR 子集指标 P5 单独统计。

## 4. 对 P1 的直接输入

- evidence_mode 需保留:`lines/line_positions`(解析期真实行号)→ chunk 的 positions 与 SourceSpan;
- 经历级 chunk 增加 `resume_scope_id_kwd / resume_scope_kind_kwd / resume_source_ref_id_kwd`;
- project chunk 绑定 parent work scope(仅原文明示或结构可核实时);
- Summary chunk 标记 `scope_kind=profile`,判断层禁止其单独作条件证据。
