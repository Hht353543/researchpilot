# ADR-0004: 混合检索 + IDF 重排 + 可插拔向量存储

- 状态：已采纳
- 日期：2026-09-13

## 背景

企业知识库检索同时存在两类需求：语义改写（dense 优势）与精确术语/编号/错误码（BM25 优势）。
同时项目必须在无网络、无模型下载的环境下可运行。

## 决策

1. 默认 embedding 使用确定性特征哈希（unigram + CJK bigram + char trigram），维度可配置；
2. 保留 `OpenAIEmbedder` 与 `SentenceTransformerEmbedder` 作为可插拔实现；
3. 检索采用 dense + BM25 的 RRF 融合，再以 IDF 加权覆盖度重排（可选 LLM 重排）；
4. chunk 内容包含章节路径，从而使标题信息参与检索与重排。

## 结果

优点：零依赖可运行；BM25 与 dense 互补；重排信号可解释（components 里保留各分项，便于归因）。

代价：哈希向量的语义泛化弱于真实 embedding，离线模式的 Context Relevance 偏低（实测 39.4%），
这正是"真实 embedding + 交叉编码器重排"被列入 Future Work 的原因。
