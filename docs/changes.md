# RAGFlow fork 相对 v0.27.2 的改动说明(交付物)

补丁位于 `ragflow-fork/patches/`,注入方式 `deploy/apply_patches.sh`(容器 recreate 后必须重跑)。

## 0000-baseline-repair-llmbundle.patch(基线修复,非功能增强)
`rag/app/resume.py`:v0.27.2 的 `_call_llm` 将字符串 `LLMType.CHAT` 传给要求 dict 的
`LLMBundle`,导致经历抽取 100% 抛 TypeError 并静默降级为仅基本信息 chunk(见
`docs/UPSTREAM_BUGS.md` BUG-001)。改为经 `get_tenant_default_model_by_type`
解析租户默认 chat 模型配置后传入。

## 0001-embedding-batch-configurable.patch(基础设施修复)
`rag/llm/embedding_model.py`:`OpenAIEmbed` 批大小硬编码 16,DashScope compatible-mode
上限 10,多 chunk 文档 embedding 400 FAIL(BUG-002)。改为环境变量
`RAGFLOW_EMBED_BATCH_SIZE` 可调(默认 16 保持上游行为),部署设 10。

## 0002-evidence-mode.patch(核心二开:证据化解析)
`rag/app/resume.py`,默认关闭(`parser_config.evidence_mode` 或环境变量
`RESUMETRACE_EVIDENCE_MODE=1` 开启;关闭时行为与上游逐字节一致):
1. `parse_with_llm` 保留抽取期的行号指针:`_work_exp_details[i].desc_lines` 与新增
   `_project_exp_details`(name/role/dates/desc_lines)。
2. `_postprocess_resume(evidence_mode=True)` 跳过 Phase 3.5 的 project→work 描述合并,
   保留 work/project scope 边界(Phase 3.4 去重已同步过滤 details,索引对齐不变)。
3. 新增 `_build_evidence()`:由行号指针构建 `scopes`(work/project/education/profile)与
   `spans`(line_start/end、真实 rects、quote、quote_hash、extraction_mode);
   行号越界/缺坐标的 span 直接丢弃,scope 标 unlocated,**不做生成后反向模糊匹配**;
   extraction_mode 由文本层有无判定(metadata/OCR)。
4. `_build_chunk_document(evidence=...)`:chunk 写入可检索字段
   `resume_scope_id_kwd / resume_scope_kind_kwd / resume_source_ref_id_kwd /
   resume_span_status_kwd / resume_span_quote_ltks`;located chunk 的 `position_int`
   使用解析期真实矩形替换上游的序号模拟坐标;unlocated chunk 保留逻辑排序坐标并显式标记。
5. `chunk()` 从 `parser_config`/环境变量读取开关并透传。

ES 侧无需改 mapping:`*_kwd` 后缀走动态 keyword 映射,集成测试
`eval/runner/it_p1_evidence.py` IT-ES-01/02/03 验证入库、回读与类型。

## 已知限制(未解决清单)
- 判断层(deepseek-v4.1-flash)存在 run 间漂移,JD 回归靠 few-shot+重试稳定到 3/3;
  语义判断准确率未达人工水平,逐条件一致率见 `eval/results/p5_compare/summary.json`。
- Web UI 当前为 `web/index.html` 单页 PDF.js 查看器;规格固定的 React/TS 版延后。
- project 与 work 的父子关系(parent_scope_id)仍为空:仅当 JD/用户确认关系限制时
  由判断层以 SAME_PROJECT/WORK_PROJECT_CHAIN 约束使用,不靠时间重叠或公司同名自动补齐。
- 200 份合成简历与双人标注未做(当前 20 份跑通集);P5 指标为跑通集测量,非验收结论。
