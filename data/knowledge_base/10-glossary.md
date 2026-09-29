---
id: kb-glossary
title: 术语表
source: internal://handbook/glossary
topic: reference
tags: [glossary, reference]
created_at: 2026-01-05T08:00:00Z
---
# 术语表

Agent：能够自主规划、调用工具并根据反馈调整行为的 LLM 应用形态。

Tool Calling / Function Calling：模型以结构化参数调用外部能力的方式。

Structured Output：约束模型输出符合 JSON Schema，便于程序消费与校验。

RAG：Retrieval-Augmented Generation，先检索后生成的范式。

Chunk：文档切分后的检索单元。

Rerank：对初筛结果做更精细的相关性重排。

Hybrid Search：融合语义检索与关键词检索的策略。

MCP：Model Context Protocol，标准化模型应用与工具/数据源交互的协议。

Trace：一次任务中所有 Agent、LLM、工具与检索调用的结构化记录。

Golden Dataset：用于回归评测的标注样本集合。

Citation Correctness：引用既能解析到真实来源，又能支撑对应结论的程度。
