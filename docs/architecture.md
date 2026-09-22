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
5. `VerifierAgent`：LLM 给出初判 → 代码复核（引用存在性、quote 归属性、topical relevance、来源质量）
6. `CriticAgent`：找缺口 → `needs_more_research` + `follow_up_queries`
7. 若需要且未超过 `max_iterations`：`ResearchAgent` 补检 → 重新校验与批评
8. `WriterAgent`：基于可用证据写作 → 引用绑定校验（剔除不存在的引用、排除含注入特征的证据）
9. 先落盘 result/trace 产物，最后提交 `runs/{task_id}.task.json` 终态；长期记忆写入主题与高置信结论

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

设计原则：Agent 状态不落在自然语言里。这样才有参数校验、版本演进、重试恢复与自动评测的可能。

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

调用方（agent runtime）可以通过 `KnowledgeBase.search(rerank_strategy=...)` 覆盖重排策略；
`knowledge_search` 工具始终传入运行时的 `settings.rerank_strategy`，因此「注入一个用别的 Settings
构建的 KnowledgeBase」不会悄悄改变重排行为。

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

### 5.1 参数策略属于工具，而不是 Agent

早期实现里 `ResearchAgent` 用 `if tool_name == "knowledge_search" / "web_search" / ...` 构造参数。
现在每个工具实现 `build_arguments(ToolRequestContext) -> dict | None`：

- 新增工具不需要修改任何 Agent；
- 工具可以表达"我现在无法被调用"（例如 `document_reader` 没有 `doc_id` 时返回 `None`）；
- 故障注入包装器会转发该策略，因此注入的超时/失败/空结果一定作用在真实调用上；
- `tests/unit/test_tool_arguments.py` 对全部内置工具与该契约做了断言。

### 5.2 缓存键的完整性

缓存键使用完整参数的 SHA-256（而不是写入 Trace 时被截断到 600 字符的副本）。否则两个共享前缀的
长查询会命中彼此的缓存结果——这是一个真实的正确性 Bug，已有回归测试。

### 5.3 未显式传递 Tracer 时

流水线在 `_execute` 中用 `tracer_scope(tracer)` 把 Tracer 设为上下文变量，
`ToolRegistry` 在 `ctx.tracer` 为空时回退到 `current_tracer()`。
因此"只拿到 ToolContext 的嵌套调用"也会产生 span，而不是静默丢失 Trace。

MCP Server 复用同一套 KnowledgeBase / WebBackend，把能力以协议方式暴露；Agent 侧通过
`mcp_research_context` 工具消费它。三种传输（in-process / stdio / HTTP）共享同一份 `McpServer` 逻辑，
因此协议行为一致，测试可分别在三种传输上验证。

## 6. 失败处理

任务生命周期由进程内 `ResearchPipeline` 统一管理：API 同步请求和异步提交都进入同一受管 worker 路径，
每个任务拥有 `TaskLifecycle` cancellation/deadline token、一个 worker 引用和一个 deadline timer。
合法主路径是 `pending → running → completed|failed|cancelled|timed_out`；terminal transition 在锁内提交，
因此 cancel/timeout/complete/failure 竞争时只有第一个成功转换者能发布结果和释放 capacity slot。

资源所有权保持显式：ServiceContainer 创建的 provider 与 MCP client 属于应用，由 FastAPI lifespan shutdown
关闭；请求覆盖模型时创建的 provider、默认 ToolRegistry 及其 executor 属于任务，由 worker 的 `finally`
关闭；注入的共享 provider、MCP client 和 registry 不会被单个任务关闭。stdio client 拥有其子进程与 reader
thread，RPC 超时或 close 时执行 terminate、短暂等待、必要时 kill，并关闭三条 pipe。

`runs/{task_id}.task.json` 是生命周期事实源，记录 created/started/finished/updated 时间、owner PID 与实例 id、
错误摘要以及 result/trace 的引用和提交状态。worker 定期刷新 `updated_at`。服务启动时在逐任务跨进程锁内扫描
`pending/running`：活跃且租约新鲜的 owner 保持不变，失联或租约过期的任务收敛为 `failed`。多个实例同时
启动时只有一个能完成该 compare-and-swap 恢复。
启动扫描也会为旧版本仅有 `.result.json` 的任务补建 task record；旧的 `pending/running` 结果随后按同一规则
收敛，避免升级后遗留永久僵尸任务。

终态采用“产物优先、任务记录最后”的提交顺序。`completed` 只有在 result 已持久化后才能提交；trace 失败
允许任务以 `completed/degraded` 收敛，并在 task record 中标为 `unavailable`。result 提交失败会把执行收敛
为 `failed`。如果最后的 task record 提交失败，调用方收到明确存储失败，磁盘上的非终态由下次启动恢复。

| 失败 | 处理 |
| --- | --- |
| LLM 输出非 JSON / 不符合 Schema | 结构化运行器带修复重试（上限 `max_retries`），失败后走确定性回退 |
| LLM 提供商不可用 | Planner 回退到确定性计划；其余 Agent 回退到抽取式结果；任务 `completed`、质量 `degraded` |
| 工具超时 | Registry 记录失败 + retry span，Agent 继续其它工具；任务 `completed`、质量 `degraded` |
| 工具预算耗尽 | 拒绝调用并记录，报告仍产出且显式标注缺口 |
| 无检索结果 | Verifier 判定证据不足 → Critic 要求补检 → Writer 在 limitations 声明缺口 |
| 低质量来源 | 来源质量评分下降 → `sufficient=false` → 报告显式提示证据不足 |
| 提示注入 | 外部内容标记为不可信数据；含注入特征的证据被排除出结论并在 limitations 中列出 |
| 引用编造 | Writer 后置校验剔除不存在的引用，记录 `dropped_citations` |
| 空知识库 | 回退到 Web/MCP 等其它来源，并把缺口写入 `Bundle.gaps` 与报告 limitations |
| 所有来源都为空 | 生命周期为 `completed`、质量为 `degraded`，报告显式声明没有可用证据 |
| 向量库/Embedding 不可用 | 工具层捕获为 tool failure，规划阶段读取 KB 统计的异常也被降级处理，任务继续 |
| Web 检索 URL 不安全 | 只接受 http/https、拒绝 URL 内嵌凭据、可选 host allow-list（SSRF 加固） |

## 7. 取舍（Trade-offs）

1. 同步执行 + 线程池：实现简单、可测试；代价是并发受线程池限制。异步并行子任务列入 Future Work。
2. 默认哈希向量：离线可运行、确定性、零依赖；代价是语义精度不如真实 embedding（可通过配置替换）。
3. 代码判定 + LLM 判定分离：Verifier 的最终结论由代码推导，LLM 只提供初判；可复现、可回归，
   代价是无法像人类评审那样理解深层语义（因此真实评测仍是必要的）。
4. 自带 JSON-RPC MCP 实现：不引入额外 SDK 依赖、可精确控制错误与超时；代价是需要自己跟进协议演进。
5. JSON 持久化：便于检查与演示；大规模场景应替换为数据库/对象存储（见 Future Work）。

## 7.1 指标诚实性（Evaluation honesty）

早期版本的 `_recall([])` / `_f1([])` 返回 `1.0`，聚合时又对所有任务取平均——没有期望值的任务
（例如 no-result 场景）会贡献"真空 1.0"，从而抬高 Retrieval / Tool-selection 指标。现在：

- 每个 `TaskJudgement` 记录 `retrieval_applicable` / `citation_applicable` / `tool_selection_applicable`；
- 聚合只在适用的任务上取平均，并在报告中给出分母（`docs/evaluation.md` 每次运行自动重算）；
- 分母为 0 时渲染为 `n/a`（而不是 `0%` 或 `100%`）；
- `Tool Success Rate` 在没有任何工具调用时为 `n/a`。

生命周期由 `pending → running → completed|failed|cancelled|timed_out` 状态机管理；结果质量单独由
`ResearchPipeline._quality` 计算。取消 token 和 wall-clock deadline 会在 Agent、模型、工具、迭代边界检查，
首次 terminal transition 获胜，late completion 不会覆盖已提交终态。

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
