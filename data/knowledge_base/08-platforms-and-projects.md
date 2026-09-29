---
id: kb-platforms-projects
title: 代表性项目与平台
source: internal://research/landscape
topic: landscape
tags: [landscape, frameworks, products]
created_at: 2026-03-20T08:00:00Z
---
# 代表性项目与平台

## 开源编排框架

LangGraph：以图结构表达 Agent 状态机，支持持久化检查点与人工介入，适合需要可控流程与恢复能力的生产系统。代价是抽象较多、上手成本偏高。

AutoGen（Microsoft）：以多 Agent 对话为核心，支持代码执行与群聊式协作，适合研究型与自动化任务；需要额外约束以避免收敛困难与成本失控。

CrewAI：以角色（Role）与任务（Task）为中心的轻量级多 Agent 框架，学习曲线平缓，适合快速搭建流水线型协作。

LlamaIndex：检索与数据连接能力突出，RAG 相关组件丰富，常用于知识密集型应用。

Haystack：面向检索管线的工程化框架，组件边界清晰，便于做检索评测与替换。

Semantic Kernel：微软推出的编排 SDK，强调与企业既有编程栈集成，支持插件式工具注册。

## 厂商与平台产品

OpenAI Agents SDK：提供 Agent、工具、会话与追踪原语的轻量 SDK，配合其 Responses API 使用。

Claude Agent SDK / Claude Code：把 Agent 循环、工具执行与上下文管理封装成 SDK，MCP 为一等公民。

Microsoft Copilot Studio 与 Salesforce Agentforce：面向企业的低代码 Agent 构建平台，优势是与既有 CRM/办公套件深度集成，限制是定制自由度与模型选择空间。

Dify、Coze 等：可视化编排与知识库托管，适合快速验证与内部工具搭建。

## 组件级项目

MCP（Model Context Protocol）生态：提供工具与数据源的标准接入层。

向量数据库（Milvus、Qdrant、pgvector、Chroma）与检索库（FAISS）：承载语义检索。

评测与可观测工具（Ragas、DeepEval、LangSmith、Langfuse、OpenTelemetry）：分别覆盖检索质量评测、回归测试与链路追踪。
