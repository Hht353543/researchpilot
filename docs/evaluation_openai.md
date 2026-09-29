# Evaluation Report

本文件由 `scripts/run_benchmark.py` 在真实运行后自动生成，里面的数字来自本次运行。

## 运行信息 (Run Metadata)

| 项 | 值 |
| --- | --- |
| run_id | `eval_fd2117ab4574` |
| 生成时间 (UTC) | 2026-09-13T10:05:44Z |
| LLM provider | `openai` |
| 模型 | `Qwen/Qwen1.5-0.5B-Chat` |
| 运行模式 | live model |
| 数据集 | `eval\golden_dataset.jsonl` |
| 总耗时 | 410.1s |
| Python / Platform | 3.12.2 / Windows-11-10.0.26200-SP0 |
| Embedding | openai (dim=768) |
| 检索 top_k | 6 |
| MCP transport | inprocess |
| Web search mode | offline |

## 数据集 (Golden Dataset)

- 任务数：35
- 含期望来源文档的任务：30
- 含期望工具的任务：35
- 含故障注入的任务：4

| 类别 | 任务数 |
| --- | --- |
| bad_source | 2 |
| citation | 3 |
| mcp | 3 |
| multi_agent | 3 |
| multi_step | 3 |
| no_result | 3 |
| prompt_injection | 3 |
| rag | 3 |
| simple_fact | 3 |
| timeout_recovery | 2 |
| tool_abuse | 4 |
| tool_calling | 3 |

## 总体指标 (Overall Metrics)

| 指标 | 值 | 说明 |
| --- | --- | --- |
| Task Success Rate | 100.0% (1/1) | 通过全部硬性判定的任务比例 |
| Retrieval Recall@6 | 100.0% | 期望来源文档出现在检索结果中的比例（分母 1 个含期望来源的任务） |
| Context Relevance | 12.5% | 检索结果中相关文档占比 (precision@k)，同一分母 |
| Citation Correctness | 100.0% | 引用可解析且能支撑结论的比例（分母 1 个要求引用的任务） |
| Tool Selection Accuracy | 100.0% | 期望工具被调用的比例（分母 1 个含期望工具的任务） |
| Tool Selection F1 | 40.0% | 工具集合的 F1，同一分母 |
| Tool Success Rate | 100.00% | 1 - 失败调用 / 总调用（5 次调用，含故障注入） |
| Avg Latency | 410.12s | 端到端平均耗时 |
| P50 / P95 Latency | 410.12s / 410.12s | 延迟分布 |
| Token Usage (total) | 71,304 | 输入 + 输出 token (估算/上报) |
| Avg Tokens / Task | 71,304.0 | 单任务平均 token |
| Cost (total) | $0.000000 | 按 `configs/pricing.yaml` 计价 |
| Avg Cost / Task | $0.000000 | 离线 mock provider 成本为 0 |
| Tool Calls / Failures | 5 / 0 | 含故障注入任务 |
| LLM Calls / Retries | 20 / 11 | 重试来自 Schema 修复与工具重试 |
| Avg Iterations | 1.00 | 研究迭代轮数 |

## 分类指标 (Per-category)

| 类别 | 任务 | 通过 | 成功率 | 平均延迟 | 平均 token | 引用正确率 | 召回率 | 工具 F1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| simple_fact | 1 | 1 | 100.0% | 410.12s | 71,304 | 100.00% | 100.00% | 40.00% |

> 说明：Recall / Context Relevance 仅在「声明了期望来源文档」的任务上取平均（1/1 个任务）；Citation Correctness 仅在要求引用的任务上取平均（1/1）；Tool Selection 仅在声明了期望工具的任务上取平均（1/1）。没有期望值的任务不再贡献「真空 1.0」，也不会稀释或抬高这些指标。

## 逐任务结果 (Per-task)

| 任务 | 类别 | 通过 | 状态 | 延迟 | token | 工具调用/失败 | 引用正确率 | 失败检查 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `fact-01-mcp-protocol` | simple_fact | 通过 | degraded | 410.12s | 71,304 | 5/0 | 100.0% | - |

## 指标定义 (Metric Definitions)

- Task Success Rate：任务通过全部硬性判定（报告存在、关键词覆盖、引用可解析、无禁用内容、缺口显式声明、故障处理符合预期等）才计为成功。
- Retrieval Recall@k：期望来源文档出现在 top-k 检索结果中的比例（数据集提供弱标注），仅在含期望来源的任务上取平均。
- Context Relevance：top-k 检索结果中属于期望文档集合的比例。
- Citation Correctness：Markdown 中 `[E#]` 引用能解析到已登记证据、且该证据映射到真实来源的比例（分母为要求引用的任务数）。
- Tool Selection Accuracy / F1：实际调用工具集合与数据集期望工具集合的召回与 F1（分母为声明期望工具的任务数）。
- Tool Success Rate：1 - 工具失败次数 / 工具调用总次数（故障注入任务会主动制造失败）；没有任何工具调用时记为 `n/a` 而不是 1.0。
- Latency / Token / Cost：来自统一 Trace 的端到端测量与真实 token 计数/估算，成本按配置文件价格表计算。

## 复现方式 (Reproduce)

```bash
# 离线确定性模式（无需 API Key，CI 使用）
python scripts/run_benchmark.py --provider mock

# 真实模型模式（需要 API Key）
export API_KEY=sk-...        # 或 RESEARCHPILOT_API_KEY
export MODEL=gpt-4o-mini
export BASE_URL=https://api.openai.com/v1
python scripts/run_benchmark.py --provider openai
```

## 数据说明

- 本次运行使用 `openai` provider（live model）。Mock provider 是确定性脚本模型，用来验证工程管线、回归与 CI；它的数字衡量检索、工具、校验、追踪、评测这些环节，不代表前沿模型的生成质量。
- `web_search` 在离线模式下检索 `data/web_corpus` 中的合成示例语料（元数据标记 `synthetic: true`），用于让系统在无网络环境可完整运行；它不是真实搜索结果。
- 使用真实模型时，请用 `--provider openai` 重新运行本脚本，本文件会被覆盖，之前的数字不会被保留或美化。
