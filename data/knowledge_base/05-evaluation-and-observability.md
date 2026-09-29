---
id: kb-evaluation-observability
title: Agent 评测与可观测性
source: internal://handbook/evaluation
topic: evaluation
tags: [evaluation, metrics, tracing, golden-dataset]
created_at: 2026-03-12T08:00:00Z
---
# Agent 评测与可观测性

## Golden Dataset

评测集需要覆盖能力边界而不是只覆盖成功路径。建议包含：简单事实检索、多步骤研究、RAG 检索、工具调用、MCP 调用、多 Agent 协作、引用正确性、提示注入、工具滥用、超时恢复、无结果场景与错误来源场景。每条样本包含问题、期望来源、期望工具与判定方式。

## 核心指标

任务成功率（Task Success Rate）：按预先定义的硬性判定（Schema 合法、引用存在、关键事实覆盖）统计。

检索召回率（Retrieval Recall@k）：期望来源文档出现在 top-k 检索结果中的比例。

上下文相关性（Context Relevance）：返回的片段中真正有助于回答问题的比例。

引用正确率（Citation Correctness）：报告中的引用既能解析到真实来源，又能支撑对应结论的比例。

工具选择准确率（Tool Selection Accuracy）：实际调用工具集合与期望工具集合的匹配程度。

工具成功率、平均延迟、token 用量与成本：用于工程容量规划。

## 真实测量原则

指标必须来自真实运行。禁止写"准确率 99%"这类无出处的数字。离线运行（确定性 Mock 模型）用于验证工程管线与回归，真实模型运行用于衡量生成质量，两者必须在报告中区分标注。

## Trace 设计

统一 Trace 应呈现 Task → Agent → (LLM | Tool | Retrieval | Retry) → Final Result 的树形结构，并记录 task_id、agent、start/end time、latency、token 用量、model、tool、input、output、error。Trace 是排查 Agent 行为的主要依据，也是评测的原始数据来源。
