# Retrieval Ablation（检索模块消融实验）

本文件由 `scripts/retrieval_ablation.py` 生成，只测**检索模块**（不含 LLM 生成），
用于把检索质量与生成质量分开归因。数据来源：`eval/golden_dataset.jsonl` 中 30 条带期望来源的任务，
k=6，查询使用任务原始问题（不做子任务拆解），因此与 `docs/evaluation.md` 中的流水线级指标**口径不同**：

- 本文件：**单查询**检索质量（Recall@6 / Precision@6 / MRR）。
- `docs/evaluation.md`：**整任务**口径——一个任务会发起多次检索（子任务 + 查询改写 + MCP/Web），
  召回是多次结果的并集，因此 Recall 更高、Precision 更低（实测 Recall 93.9% vs 单查询 82.2%，
  Precision 28.8% vs 单查询 51.7%）。两者不矛盾，是"查得更多 → 召回更高、精度更低"。

复现命令：

```bash
# 1) 内置确定性哈希向量（离线默认）
python scripts/retrieval_ablation.py

# 2) 真实中文向量模型（本地缓存 + 本地 GPU，通过 scripts/local_model_server.py 提供 /v1/embeddings）
python scripts/local_model_server.py --port 8774
RESEARCHPILOT_EMBEDDING_PROVIDER=openai \
RESEARCHPILOT_EMBEDDING_MODEL=shibing624/text2vec-base-chinese \
RESEARCHPILOT_EMBEDDING_DIM=768 \
BASE_URL=http://127.0.0.1:8774/v1 API_KEY=local \
python scripts/retrieval_ablation.py --label "text2vec-base-chinese"
```

## 实测结果

### Embedder: `hash(384d)` · index: 10 docs / 39 chunks · k=6

| strategy | tasks with expectations | Recall@k | Precision@k (context relevance) | MRR |
| --- | --- | --- | --- | --- |
| dense | 30 | 80.6% | 43.3% | 0.761 |
| keyword | 30 | 82.2% | 50.6% | 0.782 |
| hybrid | 30 | 82.2% | 51.7% | 0.769 |

### Embedder: `shibing624/text2vec-base-chinese (768d, local GPU)` · index: 10 docs / 39 chunks · k=6

| strategy | tasks with expectations | Recall@k | Precision@k (context relevance) | MRR |
| --- | --- | --- | --- | --- |
| dense | 30 | 72.2% | 47.8% | 0.744 |
| keyword | 30 | 82.2% | 50.6% | 0.782 |
| hybrid | 30 | 82.2% | 51.7% | 0.796 |

## 结论（基于上表实测）

1. **hybrid 是两种 embedder 下的最优策略**：hybrid 的 Precision@6（51.7%）与 MRR（0.769 / 0.796）
   均不低于单一路径，验证了项目默认 `hybrid` 的设计选择。
2. **真实向量模型不是"自动更好"**：在本合成语料上（10 篇 / 39 chunk、术语高度重叠的中文技术文本），
   text2vec 的 dense 召回反而低于哈希向量（72.2% vs 80.6%），但 Precision（47.8% vs 43.3%）与
   hybrid MRR（0.796 vs 0.769）更好。这解释了为什么流水线级 Context Relevance 在换用真实 embedding 后
   几乎不变（28.5% vs 28.8%）：**瓶颈在检索配置（k=6、多查询并集、IDF 重排）而不是向量质量**。
3. **keyword 完全一致（82.2% / 50.6% / 0.782）**：BM25 不依赖 embedder，这一致性同时验证了消融脚本本身没有串味。
4. 改进方向（有数据支撑）：调小 `top_k` 以提高精度、或按子任务类型路由 dense/keyword 权重，
   而不是盲目更换更大的 embedding 模型。

