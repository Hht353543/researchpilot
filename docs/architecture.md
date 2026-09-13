# Architecture

本文档描述 ResearchPilot 的组件边界、数据流、关键取舍与失败处理策略。

## 1. 分层视图

```
┌──────────────────────────── Interface ────────────────────────────┐
│ FastAPI (researchpilot/api)   CLI (researchpilot/cli.py)   MCP     │
└──────────────┬───────────────────────┬───────────────────┬────────┘
               │                       │                   │
┌──────────────▼───────────────────────▼───────────────────▼────────┐
│                      Orchestration (pipeline.py)                  │
│  ResearchPipeline: 迭代控制 / 预算 / 降级 / 持久化 Trace+Result    │
└──────────────┬────────────────────────────────────────────────────┘
               │
┌──────────────▼──────────────────── Agents ────────────────────────┐
│ PlannerAgent · ResearchAgent · VerifierAgent · CriticAgent ·       │
│ WriterAgent        （共享 ResearchRuntime：LLM / Tools / Memory）  │
└──────┬────────────┬─────────────┬───────────────┬─────────────────┘
       │            │             │               │
┌──────▼─────┐ ┌────▼─────┐ ┌─────▼──────┐ ┌──────▼──────┐
│ LLM 抽象   │ │ Tools    │ │ RAG        │ │ Memory      │
│ provider + │ │ Registry │ │ KB index + │ │ 3 层记忆    │
│ structured │ │ + MCP    │ │ Retriever  │ │ + TTL/去重  │
└────────────┘ └──────────┘ └────────────┘ └─────────────┘
       │
┌──────▼──────────────────── Observability ─────────────────────────┐
│ Tracer (span 树) → TraceStore (runs/*.trace.json) → API/UI/Eval   │
└───────────────────────────────────────────────────────────────────┘
```

## 2. 数据流（一次研究任务）

1. `POST /research` → `ServiceContainer.run_research` → `ResearchPipeline.run`
2. 创建 `Tracer`（task_id/trace_id），并按请求覆盖 Settings（model / temperature / top_k / …）
3. `PlannerAgent`：调用 LLM（结构化输出 `ResearchPlan`）→ 代码校验工具可用性、子任务数量、synthesis 子任务
4. `ResearchAgent`（迭代 1）：逐子任务 → `ToolRegistry.invoke` → 观测结果 → LLM 抽取 `EvidenceBundle`
   - 工具观测同时写入工作记忆（去重）与来源注册表（source_id → SourceRef）
5. `VerifierAgent`：LLM 给出初判 → **代码复核**（引用存在性、quote 归属性、topical relevance、来源质量）
6. `CriticAgent`：找缺口 → `needs_more_research` + `follow_up_queries`
7. 若需要且未超过 `max_iterations`：`ResearchAgent` 补检 → 重新校验与批评
8. `WriterAgent`：基于可用证据写作 → **引用绑定校验**（剔除不存在的引用、排除含注入特征的证据）
9. 落盘 `runs/{task_id}.result.json` 与 `runs/{task_id}.trace.json`；长期记忆写入主题与高置信结论

## 3. 关键 Schema（Agent 状态）

| Schema | 作用 | 关键字段 |
| --- | --- | --- |
| `ResearchPlan` | 结构化研究计划 | `objective`, `subtasks[]`, `requires_*`, `max_iterations` |
| `Subtask` | 子任务 | `id`, `question`, `intent`, `tools[]`, `expected_output`, `priority` |
| `Evidence` | 原子证据 | `id`, `subtask_id`, `claim`, `quote`, `source_id`, `confidence` |
| `SourceRef` | 来源元数据 | `id`, `kind`, `title`, `locator`, `doc_id`, `chunk_id`, `url`, `retrieved_at` |
| `VerificationReport` | 校验结论 | `checks[]`, `unsupported_claims[]`, `flagged_sources[]`, `coverage_score`, `sufficient` |
| `CritiqueReport` | 批评与补检 | `issues[]`, `needs_more_research`, `follow_up_queries[]`, `coverage` |
| `FinalReport` | 最终报告 | `conclusions[]`, `sections[]`, `recommendations[]`, `limitations[]`, `markdown` |
| `Span` / `Trace` | 可观测性 | `kind`, `agent`, `tool`, `model`, `latency_ms`, `usage`, `input`, `output`, `error` |

设计原则：**Agent 状态不落在自然语言里**。这样才有参数校验、版本演进、重试恢复与自动评测的可能。

## 4. RAG 细节

### 4.1 分块

`MarkdownChunker` 按标题层级维护章节路径，chunk 内容以 `文档 > 小节` 开头，并按句号/分号边界切分，
携带一段重叠窗口。这个选择解决两个具体问题：

1. 标题信息参与检索（否则"三种主流控制流"这类小节标题无法作为检索信号）；
2. 引用 quote 不会从句子中间开始（否则 `JSON-RPC 2.0` 这类关键术语会被截断成 `RPC 2.0`）。

### 4.2 检索

| 阶段 | 实现 | 说明 |
| --- | --- | --- |
| 查询改写 | `Retriever._rewrite` | 离线 provider 为确定性改写；真实模型可用 LLM 改写 |
| 召回 | `VectorStore.dense_search` / `keyword_search` | 哈希/真实向量 + Okapi BM25（jieba 分词） |
| 融合 | `VectorStore.hybrid_search` | RRF（`k=60`）融合两路排名 |
| 过滤 | `_matches` | 元数据等值/集合匹配（topic、tags、语言、密级…） |
| 重排 | `Retriever._rerank_heuristic` / `_rerank_with_llm` | IDF 加权的正文/标题覆盖度 + 召回分数 |
| 上下文 | `Retriever.format_context` | 带 chunk id 的上下文块，受 `max_context_chars` 约束 |

### 4.3 可评测性

检索的每一阶段都写入 Trace（`rewritten_query`、`doc_ids`、`hits`、`candidates`），
因此 `Retrieval Recall@k` 与 `Context Relevance` 可以从 trace 直接计算，而不需要重新跑一遍。

## 5. 工具与 MCP

```
Agent ──► ToolRegistry.invoke(name, args, ctx)
              ├── 权限策略检查（allow-list / deny-list / 权限等级）
              ├── 调用预算检查（每任务上限，抵御工具滥用）
              ├── 参数 Schema 校验（Pydantic）
              ├── 超时控制（ThreadPoolExecutor + timeout）
              ├── 重试（可配置，指数退避）
              ├── Trace span（tool 或 mcp）
              └── TTL 缓存（可选）
```

MCP Server 复用同一套 KnowledgeBase / WebBackend，把能力以协议方式暴露；Agent 侧通过
`mcp_research_context` 工具消费它。三种传输（in-process / stdio / HTTP）共享同一份 `McpServer` 逻辑，
因此协议行为一致，测试可分别在三种传输上验证。

## 6. 失败处理

| 失败 | 处理 |
| --- | --- |
| LLM 输出非 JSON / 不符合 Schema | 结构化运行器带修复重试（上限 `max_retries`），失败后走确定性回退 |
| LLM 提供商不可用 | Planner 回退到确定性计划；其余 Agent 回退到抽取式结果；状态标记 `degraded` |
| 工具超时 | Registry 记录失败 + retry span，Agent 继续其它工具，状态 `degraded` |
| 工具预算耗尽 | 拒绝调用并记录，报告仍产出且显式标注缺口 |
| 无检索结果 | Verifier 判定证据不足 → Critic 要求补检 → Writer 在 limitations 声明缺口 |
| 低质量来源 | 来源质量评分下降 → `sufficient=false` → 报告显式提示证据不足 |
| 提示注入 | 外部内容标记为不可信数据；含注入特征的证据被排除出结论并在 limitations 中列出 |
| 引用编造 | Writer 后置校验剔除不存在的引用，记录 `dropped_citations` |

## 7. 取舍（Trade-offs）

1. **同步执行 + 线程池**：实现简单、可测试；代价是并发受线程池限制。异步并行子任务列入 Future Work。
2. **默认哈希向量**：离线可运行、确定性、零依赖；代价是语义精度不如真实 embedding（可通过配置替换）。
3. **代码判定 + LLM 判定分离**：Verifier 的最终结论由代码推导，LLM 只提供初判；可复现、可回归，
   代价是无法像人类评审那样理解深层语义（因此真实评测仍是必要的）。
4. **自带 JSON-RPC MCP 实现**：不引入额外 SDK 依赖、可精确控制错误与超时；代价是需要自己跟进协议演进。
5. **JSON 持久化**：便于检查与演示；大规模场景应替换为数据库/对象存储（见 Future Work）。

## 8. 目录职责

| 目录 | 职责 |
| --- | --- |
| `researchpilot/llm/` | Provider 抽象、结构化输出运行器、Prompt 模板 |
| `researchpilot/agents/` | 5 个 Agent 与共享 Runtime |
| `researchpilot/tools/` | 工具契约、注册表、内置工具、Web 后端 |
| `researchpilot/rag/` | 加载/清洗/分块/向量/检索/知识库 |
| `researchpilot/mcp/` | MCP 协议、服务端、三种客户端 |
| `researchpilot/memory/` | 三层记忆 |
| `researchpilot/observability/` | Tracer、TraceStore、成本估算 |
| `researchpilot/evaluation/` | 数据集、判分、指标、runner、报告生成 |
| `researchpilot/api/` | FastAPI 应用、服务容器、前端静态资源 |
