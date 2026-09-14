# 真实模型验证（本地真实 LLM + 真实向量模型）

本文件记录真实模型在 ResearchPilot 端到端流水线上的实测结果，用于补上"只测过确定性离线 provider"的缺口。
所有数字来自真实运行，原始记录见 `benchmarks/latest_openai.json` 与
`benchmarks/real_llm_3tasks_before_writer_fallback.json`。

## 运行环境

| 项 | 值 |
| --- | --- |
| 生成模型 | `Qwen/Qwen1.5-0.5B-Chat`（真实 LLM 权重，本地缓存，无网络） |
| 向量模型 | `shibing624/text2vec-base-chinese`（768 维，真实中文 embedding） |
| 设备 | NVIDIA GeForce RTX 4060 Laptop GPU（CUDA） |
| 服务方式 | `scripts/local_model_server.py`（自实现 OpenAI 兼容端点：`/v1/chat/completions` + `/v1/embeddings`） |
| 项目侧配置 | `RESEARCHPILOT_PROVIDER=openai`、`BASE_URL=http://127.0.0.1:8774/v1`、`MODEL=Qwen/Qwen1.5-0.5B-Chat`、`RESEARCHPILOT_EMBEDDING_PROVIDER=openai`、`EMBEDDING_DIM=768` |
| 生成参数 | `MAX_TOKENS=600`、`MAX_ITERATIONS=1`、`REQUEST_TIMEOUT_S=300`（为 0.5B 模型降低单次开销） |

复现：

```bash
python scripts/local_model_server.py --port 8774 --device cuda
export RESEARCHPILOT_PROVIDER=openai MODEL=Qwen/Qwen1.5-0.5B-Chat API_KEY=local \
       BASE_URL=http://127.0.0.1:8774/v1 \
       RESEARCHPILOT_EMBEDDING_PROVIDER=openai \
       RESEARCHPILOT_EMBEDDING_MODEL=shibing624/text2vec-base-chinese \
       RESEARCHPILOT_EMBEDDING_DIM=768
python scripts/verify_live_model.py --probe-only            # 1 次最小调用
python scripts/run_benchmark.py --provider openai --task-ids fact-01-mcp-protocol rag-02-hybrid-rerank
```

## 结果

### 1) 结构化输出探针（真实模型）

```
probe -> ok: schema=CritiqueReport attempts=1 tokens=520 repairs=0
```

项目自己的 `OpenAICompatibleProvider` + `StructuredLLMRunner`（JSON 抽取 → Schema 校验 → 修复重试）
一次成功驱动本地真实 LLM 产出合法 `CritiqueReport`。

### 2) 流水线端到端（3 条 Golden Dataset 任务）

| 任务 | 结果 | 状态 | 引用正确率 | 延迟 | token | LLM 调用 |
| --- | --- | --- | --- | --- | --- | --- |
| `fact-01-mcp-protocol` | 失败 | degraded | 0.0%（0/0，报告完全没有引用） | 401.6s | 73,232 | 19 |
| `rag-02-hybrid-rerank` | 失败 | degraded | 0.0%（0/0） | 510.1s | 95,657 | 22 |
| `empty-01-unknown-codename` | 通过 | degraded | 0.0% | 439.7s | 75,364 | 19 |

合计：1/3 通过，Retrieval Recall 100%，平均延迟约 450s/任务。

失败原因（已定位，不是检索问题）：0.5B 模型返回的是 schema 合法但内容为空的 `FinalReport`
（`conclusions=[]`、`sections=[]`），于是报告里没有任何 `[E#]` 引用 → 引用正确率 0/0 → 判定失败。
检索召回 100%，说明 RAG 链路正常。

### 3) 修复后的复测（同一条任务，同一模型）

发现上述问题后新增了"空报告回退"：当模型给出的报告没有任何引用绑定、而存在可用证据时，
改用确定性抽取式写作器（结论逐条绑定证据）。复测结果：

| 任务 | 结果 | 引用正确率 | 说明 |
| --- | --- | --- | --- |
| `fact-01-mcp-protocol` | 通过 通过 | 100.0% | 状态仍为 degraded（记录了"模型报告无引用，已回退"），但报告完整、引用可解析 |

合计：1/1 通过，Retrieval Recall 100%。

## 结论（诚实说明）

1. 真实 LLM 链路已验证：真实模型权重、真实向量模型、真实 HTTP 调用、真实 token 计量与真实延迟，
   全程无厂商 API Key、无外网。
2. 0.5B 模型质量不足以承担写作：它能完成单次结构化输出（探针通过），但在长上下文多步任务中
   会退化为空报告；本项目没有因此编造引用或假装成功——状态标记为 `degraded`，
   评测判分把失败暴露出来（引用正确率 0/0），修复后由确定性回退保证"有依据"。
3. 这正好验证了系统的可靠性设计：弱模型 → 降级但不幻觉；强模型（GPT-4o/DeepSeek 等）通过
   `--provider openai` + 有效 Key 可获得更高质量数字。
4. 成本参考：本地运行成本为 0；同一 3 条任务的 token 消耗（73k–96k/任务）说明
   `RESEARCHPILOT_TOKEN_BUDGET` 与 `MAX_TOKENS` 在生产中必须按模型能力调优。
