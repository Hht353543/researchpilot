---
id: kb-fixture-retrieval
title: 检索与重排
topic: rag
tags: [rag, retrieval, rerank]
created_at: 2026-01-03T00:00:00Z
---
# 检索与重排

## 混合检索

语义检索擅长同义改写，关键词检索擅长精确匹配，混合检索通过 RRF 融合两路排名。

## 重排

重排使用更强的打分函数对召回结果重新排序，常用做法是交叉编码器或模型打分。

## 评测

检索评测常用 Recall@k、Precision@k 与上下文相关性指标。
