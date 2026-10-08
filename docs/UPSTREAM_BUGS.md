# RAGFlow v0.27.2 上游缺陷记录(ResumeTrace 二开前置发现)

## BUG-001:resume.py 的 LLM 调用传参类型错误,经历抽取 100% 静默失败

- **位置**:`rag/app/resume.py:1058`(v0.27.2)`llm = LLMBundle(tenant_id, LLMType.CHAT, lang=lang)`
- **原因**:`LLMBundle.__init__(tenant_id, model_config: dict, ...)`(见 `api/db/services/llm_service.py:81`
  → `api/db/services/tenant_llm_service.py:512` `self.llm_name = model_config["llm_name"]`)。
  resume.py 传入的是字符串 `LLMType.CHAT`("chat"),`"chat"["llm_name"]` 抛
  `TypeError: string indices must be integers, not 'str'`。
  同版本其他调用方(`rag/prompts/generator.py:267`、`rag/graphrag/search.py:315` 等)均传 dict,
  仅 resume.py 未跟随 `LLMBundle` 签名变更更新。
- **表现**:每份简历解析日志出现 `WARNING ... LLM call failed: string indices must be integers, not 'str'`,
  随后 `_call_llm_for_json` 返回 None,抽取降级为空结构;最终每份简历只产出 1 个
  "Basic Info / Name: Unknown" chunk,工作/项目经历字段全部丢失。解析任务状态仍为 DONE,
  **不报错、不重试、不可见于任务状态**,只有 warning 日志可查。
- **实测证据**(2026-09-26,本机 Docker,模型 deepseek-v4.1-flash@DashScope 兼容端点):
  - `eval/runner/p0_baseline.py` 产出的 `b0_import.jsonl` + `b0_retrieval.jsonl`(本地评测产物,不入库):
    20/20 文档 DONE,但每文档 chunk 数 = 1,内容仅基本信息;所有检索命中均为该基本信息 chunk。
  - 容器内复现堆栈:`LLMBundle(tid, LLMType.CHAT)` → TypeError(tenant_llm_service.py:512)。
- **处置**:`ragflow-fork/patches/0000-baseline-repair-llmbundle.patch`,改为
  `LLMBundle(tenant_id, get_tenant_default_model_by_type(tenant_id, LLMType.CHAT), lang=lang)`。
  该补丁是**基线修复**,不含任何证据化能力;评测对照以修复后版本为 B0(`eval/results/b0r_*`),
  损坏版测量保留为 `b0_*` 作为缺陷证据。证据化二开补丁(0001+)与本修复相互独立,消融不受影响。
- **影响范围**:v0.27.2 宣称的工作/项目经历抽取在**代码层面**存在、在**运行层面**不成立
  (该版本 resume 解析器实际不可用)。任何以"上游已提供结构化抽取"为前提的下游设计,
  在未打本补丁时都拿不到经历字段;ResumeTrace 的证据化能力建立在本修复之上。

## BUG-002:OpenAI 兼容 embedding 批大小硬编码 16,DashScope(上限 10)下多 chunk 文档解析失败

- **位置**:`rag/llm/embedding_model.py` `OpenAIEmbed.encode/encode_queries`(v0.27.2)`batch_size=16` 硬编码。
- **表现**:chunk 数 >10 的文档(实测 CV17,18 chunks)embedding 请求被 DashScope 拒绝:
  `400 InvalidParameter: batch size is invalid, it should not be larger than 10`,任务 FAIL;
  单 chunk 或少 chunk 文档不受影响,故缺陷只在长文档暴露。
- **处置**:`ragflow-fork/patches/0001-embedding-batch-configurable.patch`,
  批大小改由环境变量 `RAGFLOW_EMBED_BATCH_SIZE` 控制(默认 16 保持上游行为),
  部署在 `deploy/docker-compose.yml` 的 ragflow-cpu environment 中设为 10。
- **注意**:容器 recreate 会丢失 docker cp 注入的补丁,必须重跑 `deploy/apply_patches.sh`。

## 环境备注

- 部署:预构建 amd64 镜像 + Rosetta(daocloud 镜像源);ES `MEM_LIMIT` 8GB→4GB;Docker Desktop 内存 6GB→12GB
  (ES 曾被 VM OOM kill,exit 137)。
- 模型:chat=`deepseek-v4.1-flash`、embedding=`text-embedding-v4`,均经 OpenAI-API-Compatible
  通道接 DashScope compatible-mode;租户默认已设置。
