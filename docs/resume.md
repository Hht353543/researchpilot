# 简历项目描述（依据本次真实评测数据生成）

> 数据来源：2026-09-15T09:55:46Z 的评测运行，provider=`mock`（离线确定性模式（mock provider）），数据集 35 个任务，耗时 11s。若使用真实模型重新运行 benchmark，请用新数据替换下列数字。

## 项目名称

ResearchPilot —— 多 Agent 深度研究与知识库平台

## 项目描述（简历 bullet）

- 设计并实现多 Agent 深度研究系统（Planner/Researcher/Verifier/Critic/Writer），以 Pydantic 结构化 Schema 承载全部 Agent 状态，通过 JSON Schema 约束 + 校验失败自动修复重试，在 35 条 Golden Dataset 上取得 100.0% 任务成功率（引用可解析率 100.0%）。
- 构建可评测 RAG 管线（Loader→Cleaner→Heading-aware Chunker→Embedding→Vector Store→Hybrid Retrieval→Reranker），实现语义/关键词/RRF 混合检索、元数据过滤、查询改写与重排，检索 Recall@6 达 93.9%、上下文相关性 28.8%。
- 实现统一 Tool Registry（Schema 校验 + 权限分级 + 超时 + 重试 + Trace）与独立 MCP Server（JSON-RPC 2.0，stdio/HTTP 双传输，暴露 search_knowledge/get_document/search_web/get_research_context），工具选择召回 100.0%、工具成功率 74.62%，并用故障注入（超时/失败/空结果/调用预算）验证韧性。
- 建立真实测量的评测与可观测体系：Golden Dataset 覆盖 12 类场景（含 Prompt Injection、工具滥用、超时恢复、无结果、低质量来源），统一 Trace 记录 Task→Agent→LLM/Tool/Retrieval/Retry 的延迟与 token；端到端平均延迟 0.32s（P95 1.43s），单任务平均 18,290 token、成本 $0.000000（按价格表计价）。

## 使用说明

- 上表来自离线确定性 provider（mock），衡量的是检索、工具、校验、引用绑定、追踪、评测这些工程环节，不代表真实模型的生成质量。
- 数字可以对着 `docs/evaluation.md` 和 `benchmarks/` 下的原始 JSON 逐条核对。
- 用真实模型（`--provider openai`）重跑会生成新数据，本文件会被覆盖。
