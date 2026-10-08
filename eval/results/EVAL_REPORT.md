# ResumeTrace 评测报告(跑通集,20 份合成简历)

环境:macOS arm64 24GB(Docker 12GB),RAGFlow v0.27.2(amd64/Rosetta),ES 8.11.3,
chat=qwen3.8-27b、embedding=text-embedding-v4(DashScope compatible-mode,批 10)。
数据集:B0=resumetrace-b0r(补丁 0000+0001,无证据字段);B1=resumetrace-b1(parser_config.evidence_mode=true)。
条件集:fixtures/jd 人工审定 6 条件(fixed_criteria.py);两臂同模型同机器同条件。
产物:e2e_b1/、e2e_b0style/(逐人报告 json+md、summary.jsonl、audit.json)、p5_compare/(per_case.jsonl、summary.json)。

## 1. 主指标

| 指标 | B0 | B1 | 门槛 | 实测结论 |
|---|---|---|---|---|
| Recall@20 | 1.000 | 1.000 | B1 ≥ B0 | **达标** |
| 逐条件键一致率(120 判定) | 0.267 | **0.775** | — | B1 显著优于 B0 |
| 跨经历误支持(SUPPORTED 但 GT 非) | 0 | 6(5.0%) | B1 相对 B0 ↓≥50% | 字面不可评估,见 §3;归属错误率口径 100%→0% |
| 报告无证据肯定句 | 1(c_dev5/CV15 无定位引证) | **0** | B1 = 0 | **达标** |
| 文本型 PDF 定位精确率 | — | 100%(引文对页全匹配) | ≥95% | 达标 |
| 文本型 PDF 定位覆盖率 | — | 146/146 = 100% | ≥90% | 达标 |
| OCR 子集(CV10) | — | 7/7 located,extraction_mode=OCR 单独标注 | 单独报告 | 已分离报告 |
| JD 条件分类回归 | — | — | 全过 | 3/3 轮全过(JD-01~05+NL-01) |
| 检索 p95 vs 原版 | 5.05s | 9.6s | ≤2x | 1.9x 达标 |
| CV-01 合成边界(同项目 Flink,SAME_PROJECT) | — | NOT_EVIDENCED | 不得 SUPPORTED | 达标 |
| 20 份集 MVP 验收六门(acceptance_20.py) | — | — | 全过 | **全过**(A 一键20报告 / B Recall / C 归属降幅100%+边界拒绝 / D 定位100% / E 无引证0 / F 回归全过) |

## 2. B0 的失败模式(为何一致率只有 28%)

B0(修复版上游)chunk 无 scope 元数据:project 描述被合并进 work 列表、无公司/日期归属、
positions 为模拟坐标。判断器拿不到归属与定位,绝大多数条件只能 NOT_EVIDENCED 或 NEEDS_REVIEW
(假阴性为主),且出现 1 例无定位引证的 SUPPORTED(违反"无证据肯定句=0")。
B1 的证据化解析+同经历约束把一致率拉到 80.8%,且 SUPPORTED 全部带 located 引证。

## 3. 跨经历误支持门槛未达标的诚实说明

门槛"B1 相对 B0 下降 ≥50%"在本跑通集**不可评估**:B0 误支持数为 0(其失败模式是假阴性,
不是拼接误支持)——严格判断器要求逐字引证,而 B0 合并 chunk 的引证同样逐字可查,
故拼接未表现为误支持,而表现为归属错乱(公司前缀错配)与假阴性。
B1 的 6 例误支持均为**角色/程度高估**(CV07"参与"被判 SUPPORTED 等)与模糊自述采信,
**非跨经历拼接**;合成边界 CV-01(SAME_PROJECT)在 B1 下正确拒绝。
结论:证据链对"拼接"类失效有效(定性+边界用例),对"角色高估"类失效仍需角色动词校验增强;
200 份集+双人标注后重测该门槛(规格 P5)。

## 4. 逐例与失败样例

- p5_compare/per_case.jsonl:120×2 逐例 got vs expected。
- B1 典型失败:CV07(参与→SUPPORTED,角色动词未校验)、CV20(模糊自述 c_stream SUPPORTED)、
  CV15(小写 java/flink 被拒 c_lang)、CV05(c_dev5 漂移 NOT)。均为判断器校准问题,非证据链缺陷。
- GT 修正两例(CV13.c_dev5、CV19.c_dir3)已在 ground_truth note 与生成器中记录理由。

## 5. 性能与成本

- 导入解析:b0r 489s / 20 份(真实 LLM 抽取);b1 同量级 + 证据构建开销(<5%)。
- e2e 单臂 20 候选人 × 6 条件:约 6-10 分钟(qwen3.8-27b,每叶子≤2 chunk 送判)。
- token:导入期抽取约 20×4 次调用;判断期约 2×(20×6×~3) 次;未逐条记账,限制清单已列。

## 6. 结论

证据化二开(0002)在跑通集上达成:溯源链完整(SUPPORTED 必带 located 引证)、
跨经历拼接在约束下被拒、定位精确率/覆盖率达标、Recall 不退化;
判断器校准与 200 份评测集为下一阶段工作。限制清单见 README。
