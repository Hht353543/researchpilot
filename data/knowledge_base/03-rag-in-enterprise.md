---
id: kb-rag-enterprise
title: 企业级 RAG：从相似度检索到可评测管线
source: internal://handbook/rag
topic: rag
tags: [rag, retrieval, rerank, evaluation]
created_at: 2026-01-22T08:00:00Z
---
# 企业级 RAG：从相似度检索到可评测管线

## 完整管线

Document → Loader → Cleaner → Chunker → Embedding → Vector Store → Retriever → Reranker → Context → LLM。把 Retrieval 当作独立模块而非 `similarity_search()` 一行调用，是能否量化和优化的分水岭。

## 分块策略

固定长度分块实现简单，但会切断标题与列表语义。标题感知（heading-aware）分块保留章节路径，配合 10%~20% 的重叠窗口，能在召回与噪声之间取得较好平衡。分块元数据（section、position、characters、tokens）应随 chunk 一起存储，便于过滤与引用定位。

## 检索策略

语义检索（dense）擅长同义改写与跨语言匹配，但对专有名词、编号、错误码等精确匹配不敏感。关键词检索（BM25）恰好互补。混合检索（hybrid）通过 RRF 等排名融合算法结合两者，通常比单一策略更稳健。

元数据过滤（metadata filtering）用于限定产品线、语言、时间范围或密级，是避免"检索到错误版本文档"的关键手段。

## 重排与查询改写

重排（rerank）在召回之后用更强的打分函数重新排序，常见做法包括交叉编码器或 LLM 打分。查询改写（query rewrite）把口语化子问题扩写成包含领域术语与同义词的检索式，可提升召回率，但必须可关闭以便归因。

## 可评测性

检索评测常用 Recall@k、Precision@k、MRR 与上下文相关性。没有标注数据时，可用"预期来源文档集合"作为弱标注，衡量期望文档是否出现在 top-k 结果中。
