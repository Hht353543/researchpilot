# ResearchPilot — Multi-Agent Deep Research & Knowledge Base Platform

ResearchPilot 是一个**可运行、可评测、可观测**的多 Agent 深度研究系统：输入一个研究问题，
系统会自动拆解任务、调用工具与知识库、收集并校验证据、自我批评、补检，最后输出一份**每条结论都绑定来源**的
结构化研究报告。

它不是 Chatbot Demo，而是一套把 LLM 应用工程化落地所需的完整链路：

```
LLM → Prompt → Structured Output → Tool Calling → RAG → Knowledge Base → MCP
    → Multi-Agent → Memory → Evaluation → Observability / Trace → FastAPI → Docker → CI
```

---

## Project Overview

> 需求追溯矩阵：规格书每一条要求 → 仓库证据 → 验证状态，见 [`docs/requirements_traceability.md`](docs/requirements_traceability.md)（唯一未验证项为需要 Docker 引擎的 `docker compose up` 实机执行）。



> 代码审查与修复记录：本仓库经过一轮完整的「审查 → 定位 → 修复 → 补测试 → 运行验证 → 更新文档」，
> 结论（A–J 能力清单、八项接线专项检查、BUG/PARTIAL/MISSING/DEAD CODE/FAKE FEATURE/TECH DEBT 分类、
> 25 项发现的严重程度分布与验证证据）见 [`docs/audit_report.md`](docs/audit_report.md)。

| 能力 | 实现 |
| --- | --- |
| 多 Agent 编排 | Planner → Researcher → Verifier → Critic → Writer（结构化状态，可重试、可降级） |
| Agent 状态 | 全部使用 Pydantic Schema（`ResearchPlan` / `EvidenceBundle` / `VerificationReport` / `CritiqueReport` / `FinalReport`） |
| RAG 管线 | Loader → Cleaner → Heading-aware Chunker → Embedding → Vector Store → Hybrid Retriever → Reranker → Context |
| 检索模式 | 语义检索 / BM25 关键词检索 / RRF 混合检索 / 元数据过滤 / 查询改写 / IDF 重排（可选 LLM 重排） |
| 工具系统 | 统一 Tool Registry：Schema 校验 + 权限分级 + 超时 + 重试 + 缓存 + Trace + 调用预算 |
| 工具参数策略 | 每个 Tool 自己声明「如何把一个子任务翻译成自己的参数」，Agent 中没有任何 `if tool_name == ...` 分发 |
| MCP | 自研 MCP Server（JSON-RPC 2.0）：`search_knowledge` / `get_document` / `search_web` / `get_research_context`，支持 in-process、stdio、streamable-HTTP 三种传输 |
| Memory | 短期（token 预算滚动窗口）/ 工作记忆（计划、子任务、证据、观测）/ 长期记忆（TTL、去重、重要度、来源、命中次数持久化） |
| Evaluation | 35 条 Golden Dataset（12 类场景）+ 确定性判分 + 真实测量指标 + 自动生成 `docs/evaluation.md` |
| Observability | 统一 Trace：Task → Agent → (LLM / Tool / Retrieval / Retry)，含延迟、token、模型、输入输出、错误 |
| 服务化 | FastAPI（类型安全、参数校验、错误语义、日志）+ 零依赖前端（输入 / 模型参数 / 知识库 / 时间线 / 报告 / 指标 / 评测面板） |
| 知识库管理 | 文档入库、检索、删除（级联删除 chunk）、全量重建索引（磁盘删除的文件不会残留） |
| LLM 抽象 | 任意 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama…）+ 确定性离线 provider；真实 HTTP 链路由本地兼容端点端到端测试覆盖 |
| 工程质量 | **170 个测试**（unit / integration / evaluation，含真实 HTTP provider 链路）、ruff、mypy、pip check、Dockerfile、docker-compose、GitHub Actions |

---

## Architecture

```
                     ┌──────────────────────── FastAPI ────────────────────────┐
   POST /research →  │  ServiceContainer (KB · TraceStore · ResultStore · MCP)  │
                     └───────────────┬─────────────────────────────────────────┘
                                     ▼
                             ResearchPipeline
                                     │
   ┌─────────────┬──────────────────┼───────────────────┬──────────────┐
   ▼             ▼                  ▼                   ▼              ▼
PlannerAgent  ResearchAgent    VerifierAgent       CriticAgent    WriterAgent
   │             │                  │                   │              │
   │        ToolRegistry            │   (代码复核)       │      (引用绑定校验)
   │   ┌──────┬──────┬──────┬──────┐│                   │              │
   │   ▼      ▼      ▼      ▼      ▼│                   │              ▼
   │ 知识库  Web  文档读取 计算  元数据/MCP ◄────────────┘        FinalReport(markdown)
   │   │      │      │      │      │
   │   └──────┴──────┴──────┴──────┘
   │                ▼
   │      Memory（short / working / long-term）
   └────────────────► Trace（span 树，供 API / UI / 评测使用）
```

完整设计说明、时序与取舍见 [`docs/architecture.md`](docs/architecture.md)；
架构决策记录见 [`docs/adr/`](docs/adr/)。

---

## Agent Workflow

1. **PlannerAgent**：把问题拆成 3–6 个子任务，为每个子任务选择工具并声明期望产出；
   输出经代码校验（未知工具剔除、缺工具回退、强制存在 synthesis 子任务）。
2. **ResearchAgent**：按子任务执行工具（知识库检索 / Web / MCP / 文档 / 计算 / 元数据），
   记录观测结果，再把观测压缩为**原子化、可引用**的证据（claim + quote + source_id）。
3. **VerifierAgent**：逐条复核证据——引用是否真实存在、quote 是否来自该来源、claim 是否被 quote 支持、
   子任务覆盖度与来源质量；所有判定都在**代码里重新推导**，不盲信模型自评。
4. **CriticAgent**：找缺口与逻辑问题（覆盖度、引用、时效、冗余），必要时给出 follow-up 查询。
5. **WriterAgent**：仅基于通过校验的证据写作；**代码强制引用绑定**——任何不存在于证据集的
   `[E#]` 引用都会被剔除并记录在 `dropped_citations`；参考文献由代码从来源元数据生成。

循环控制：`max_iterations`、工具调用预算、token 预算、工具超时与重试次数共同保证 Agent 不会无限循环。

---

## RAG

- **Loader**：支持 Markdown（YAML front matter）/TXT/JSON/JSONL/CSV。
- **Cleaner**：全角归一化、HTML 噪声与样板行剔除、重复行去重，并返回压缩统计。
- **Chunker**：标题感知分块，chunk 内容以章节路径开头（`文档 > 小节`），按句边界切分并带重叠窗口——
  这让"标题信息"参与检索，也让引用 quote 不会从句子中间截断。
- **Embedding**：默认确定性哈希向量（离线可用），可切换 OpenAI 兼容 `/embeddings` 或 sentence-transformers。
- **Vector Store**：内存实现 + JSON 持久化，支持 dense / BM25 keyword / RRF hybrid 与元数据过滤。
- **Retriever**：查询改写 → 混合召回 → IDF 加权重排（可选 LLM 重排）→ 上下文组装，全过程产生 Trace。
- **Knowledge Base**：`document_id / title / source / chunk_id / content / metadata / created_at / embedding`。

检索质量由评测集量化：`Retrieval Recall@k`、`Context Relevance`（见下方 Benchmark）。

---

## Tool Calling

每个工具声明 `name / description / input schema / output schema / permission / timeout / retry policy`，
由 Registry 统一路由（**没有 if/elif 分发**）：

| tool | 作用 | 权限 |
| --- | --- | --- |
| `knowledge_search` | 混合检索知识库（支持过滤、改写、重排） | read_only |
| `web_search` | Web 检索（离线语料或真实 HTTP 后端） | network |
| `document_reader` | 按 doc_id / chunk_id 读取原文 | read_only |
| `calculator` | AST 白名单安全表达式求值 | compute |
| `metadata` | 文档/分块元数据、主题、时间、任务上下文 | read_only |
| `mcp_research_context` | 通过 MCP Server 获取打包研究上下文 | network |

每次调用都会产生 Trace span：`{tool, arguments, result, latency_ms, success}`。

---

## MCP

`researchpilot/mcp/` 实现了完整的 MCP 协议面（initialize / tools/list / tools/call）与三种传输：

```bash
# 1) stdio（Claude Desktop / Cursor 等客户端可直接接入）
python -m researchpilot.mcp_server --stdio

# 2) streamable HTTP（可独立部署、独立扩缩容）
RESEARCHPILOT_MCP_TRANSPORT=http python -m researchpilot.mcp_server

# 3) in-process（默认，用于单进程部署与测试）
```

Agent 通过同一个 Tool Registry 使用 MCP 能力，因此 **MCP Server 与 Agent 完全解耦**：
服务端只关心工具定义与实现、权限与超时；客户端只关心协议交互。

---

## Memory

| 层 | 作用 | 关键机制 |
| --- | --- | --- |
| Short-Term | 当前任务上下文 | token 预算、滚动淘汰、去重查询 |
| Working | 当前研究任务状态（plan / subtasks / evidence / observations） | 证据去重（hash + Jaccard）、缺口计算、快照 |
| Long-Term | 跨任务偏好、主题、结论、事实 | TTL、相似度去重、重要度 + 时间衰减 + 命中次数排序、来源与时间戳 |

---

## Evaluation

`eval/golden_dataset.jsonl` 包含 **35 条真实测试任务**，覆盖 12 类场景：
`simple_fact`、`multi_step`、`rag`、`tool_calling`、`mcp`、`multi_agent`、`citation`、
`prompt_injection`、`tool_abuse`、`timeout_recovery`、`no_result`、`bad_source`。

判分**全部由代码完成**（不依赖 LLM 当裁判）：报告是否存在、关键词覆盖、禁用内容、
引用可解析率、检索召回、上下文相关性、缺口显式声明、故障处理是否符合预期、
状态与延迟预算等。

```bash
python scripts/run_benchmark.py --provider mock      # 离线确定性，CI 使用
python scripts/run_benchmark.py --provider openai    # 真实模型（需 API_KEY）
```

真实模型可用一条命令完成自检与评测（脚本永不打印密钥，凭据缺失/被拒绝时给出明确退出码）：

```bash
export API_KEY=sk-...                                # 或 RESEARCHPILOT_API_KEY
python scripts/verify_live_model.py --probe-only     # 1 次最小真实调用
python scripts/verify_live_model.py --limit 35       # 真实模型跑完整 Golden Dataset
```

> 本开发环境中的 `OPENAI_API_KEY` 对 `api.openai.com` 返回 **HTTP 401 `invalid_api_key`**
> （见 `docs/audit_report.md` 的"仍然存在的问题"），因此仓库内提交的评测数字来自离线确定性 provider；
> 换成有效凭据后按上面两条命令即可生成真实模型数字（`docs/evaluation.md` 与 `docs/resume.md` 会自动重写）。

指标（Task Success Rate / Retrieval Recall / Context Relevance / Citation Correctness /
Tool Selection Accuracy & F1 / Tool Success Rate / Latency(P50,P95) / Tokens / Cost）
全部来自真实运行，并自动写入 `docs/evaluation.md`；`docs/resume.md` 中的简历 bullet 同样由脚本用实测数字生成。

---

## Observability

统一 Trace 结构：

```
Task
 ├── Agent（PlannerAgent / ResearchAgent / ...）
 │    ├── LLM（prompt/completion、model、tokens、latency）
 │    ├── Tool（名称、参数、结果条数、成功与否、重试次数）
 │    ├── Retrieval（策略、改写后的查询、命中 chunk、doc_ids）
 │    └── Retry（失败原因）
 └── Final Result
```

Trace 落盘到 `runs/{task_id}.trace.json`，通过 `GET /research/{task_id}/trace` 暴露，
前端以时间线渲染；评测指标也直接从 Trace 计算（例如工具选择、检索 doc_ids）。

---

## Installation

```bash
# 1) 安装（Python 3.11+；无网络时可只装核心依赖，默认哈希向量与 mock provider 均可离线运行）
pip install -e ".[dev]"

# 2) 构建知识库索引并运行一次研究
python -m researchpilot.cli ingest
python -m researchpilot.cli research "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。"

# 3) 启动服务（前端在 http://127.0.0.1:8000/）
python -m researchpilot.cli serve
```

---

## Configuration

所有配置均可通过环境变量或 `.env` 提供（见 `.env.example`）：

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RESEARCHPILOT_PROVIDER` | `mock`（离线确定性）或 `openai`（任意 OpenAI 兼容端点） | `mock` |
| `MODEL` / `API_KEY` / `BASE_URL` | 模型、密钥、端点（DeepSeek / vLLM / Ollama 等兼容端点同样适用） | `gpt-4o-mini` / 空 / 官方地址 |
| `TEMPERATURE` / `PRESENCE_PENALTY` / `FREQUENCY_PENALTY` / `MAX_TOKENS` | 生成参数（前端可覆盖前四项） | 0.2 / 0 / 0 / 1200 |
| `RESEARCHPILOT_TOP_K` / `RETRIEVE_K` | 最终上下文条数 / 召回候选数 | 6 / 12 |
| `RESEARCHPILOT_MAX_ITERATIONS` / `TOKEN_BUDGET` | 研究迭代上限 / 单任务 token 预算 | 2 / 80000 |
| `RESEARCHPILOT_EMBEDDING_PROVIDER` / `EMBEDDING_DIM` | `hash` 或 `openai`，向量维度 | hash / 384 |
| `RESEARCHPILOT_MCP_TRANSPORT` / `MCP_URL` | `inprocess` / `stdio` / `http` | inprocess |
| `RESEARCHPILOT_WEB_SEARCH_MODE` / `WEB_SEARCH_URL` | `offline`（内置语料）或 `http`（真实搜索 API） | offline |
| `RESEARCHPILOT_KB_PATH` / `RUNS_PATH` | 知识库目录 / 运行产物目录 | `data/knowledge_base` / `runs` |

---

## API

完整参考见 [`docs/api.md`](docs/api.md)。核心端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/research` | 运行研究（`mode=sync` 直接返回结果，`mode=async` 返回 `202` + `task_id`） |
| GET | `/research/{task_id}` | 完整结果（计划、证据、校验、报告、指标） |
| GET | `/research/{task_id}/trace` | Agent/LLM/Tool/Retrieval/Retry 轨迹 |
| GET | `/research/{task_id}/sources` | 全部引用来源 |
| GET | `/research/{task_id}/metrics` | 延迟、token、工具调用、重试 |
| GET | `/kb/documents`、`/kb/search`、`/kb/documents/{doc_id}` | 知识库浏览与检索 |
| POST | `/kb/documents`、`/kb/reindex` | 文档入库与重建索引 |
| DELETE | `/kb/documents/{doc_id}` | 删除文档及其全部 chunk |
| GET | `/mcp/tools`、POST `/mcp/call` | MCP 工具发现与调用 |
| GET | `/evaluation/latest` | 最近一次 benchmark 指标 |

---

## Docker

```bash
docker compose up --build
# API + 前端: http://localhost:8000/
# MCP Server: http://localhost:8765/health
```

`docker-compose.yml` 把 API 与 MCP Server 作为**两个独立服务**部署，API 通过 streamable-HTTP
调用 MCP，体现"协议解耦"的工程价值。

**本机没有容器引擎**（`docker`/`podman`/`buildah`/`nerdctl` 均不存在，WSL 也没有安装发行版），
因此这里没跑过 `docker compose up`。作为等价验证，仓库提供了**由 compose 文件驱动的无引擎拓扑测试**：

```bash
python scripts/compose_smoke.py      # 也可由 pytest 执行：tests/integration/test_compose_topology.py
```

该脚本读取真实的 `docker-compose.yml`（服务、`command`、`ports`、`environment` 的
`${VAR:-default}` 插值、`depends_on: service_healthy`），先起 `mcp` 并等其 healthcheck，再起 `api`，
然后断言：MCP `/health` 正常、API `/health` 报告 `mcp_transport=http`、`/mcp/tools` 走 HTTP、
前端 200、以及一次研究任务**真的通过 HTTP 调用了 MCP**（`mcp_calls=1`）。
实测输出（本机）：

```
[compose-smoke] mcp healthy: tools=4
[compose-smoke] api healthy: provider=mock mcp_transport=http kb_docs=10
[compose-smoke] research: status=succeeded mcp_calls=1 evidence=8
[compose-smoke] OK: compose topology verified without a container engine
```

容器内路径/主机名（`/app/...`、`mcp:8765`）到宿主机路径的映射会被逐条打印，保证证据透明。

---

## Testing

```bash
python -m pytest -q                       # 单元 + 集成 + 评测（170 个测试）
python -m pytest -q -m "not evaluation"   # 快速回归
python -m pytest -q -m evaluation         # 全量 Golden Dataset 冒烟
ruff check . && ruff format --check . && mypy researchpilot
python -m pip check                       # 依赖一致性
```

测试目录：`tests/unit/`（组件契约、依赖一致性、路径解析、工具参数策略、安全加固）、
`tests/integration/`（流水线、API、MCP 三种传输、**OpenAI 兼容 HTTP provider 全链路**）、
`tests/evaluation/`（数据集完整性、评测器机制、全量冒烟）、`tests/fixtures/`（样例语料、mock 数据与本地 OpenAI 兼容端点）。

前端是**零依赖 vanilla JS**（无 npm/构建步骤、无 package.json），因此没有 `npm install`；
但前端逻辑是**真被测的**：
`tests/frontend/app.test.mjs`（`node --test`，零 npm 依赖）在 VM + DOM stub 中加载 `app.js`，覆盖 Markdown 渲染、
Agent 时间线、指标卡片、知识库列表、来源与评测面板（含 HTML 转义/XSS 与空状态）；
`tests/unit/test_config_and_paths.py` 额外做前端↔OpenAPI 契约测试（前端调用的每个 URL 必须存在于路由表，
Settings/面板元素必须存在并被读取）。CI 中作为独立步骤执行 `node --test "tests/frontend/**/*.test.mjs"`。

---

## Benchmark

最近一次离线评测（`provider=mock`，确定性脚本模型，35 条任务，见 [`docs/evaluation.md`](docs/evaluation.md)）：

| 指标 | 结果 |
| --- | --- |
| Task Success Rate | **100%** (35/35) |
| Retrieval Recall@6 | 93.9%（分母 30 个含期望来源的任务） |
| Context Relevance | 28.8%（同一分母） |
| Citation Correctness | 100%（分母 34 个要求引用的任务） |
| Tool Selection Accuracy / F1 | 100% / 85.9%（分母 35 个含期望工具的任务） |
| Tool Success Rate | 74.6%（130 次调用，含主动注入的超时/失败/预算任务） |
| Avg / P95 Latency | 0.31s / 1.36s |
| Tokens (total) / Cost | 653,335 / $0.000000 |

> **必须诚实阅读这张表**：离线 `mock` provider 是*确定性脚本模型*，用于验证**工程管线**
> （检索、工具、校验、引用绑定、追踪、评测、故障恢复），因此这组数字衡量的是**系统管线正确性与回归基线**，
> **不代表前沿模型的生成质量**。真实模型质量请用 `python scripts/run_benchmark.py --provider openai`
> 重新运行，数字会自动覆盖写入 `docs/evaluation.md`，本文件中的表格也请同步替换为实测值。

---

## 真实模型验证（本地真实 LLM + 真实 embedding，已实测）

仓库不仅验证过离线确定性 provider，还用**本地真实模型**跑通了整条链路（无厂商 Key、无外网）：

- 生成模型：`Qwen/Qwen1.5-0.5B-Chat`（真实权重，GPU）
- 向量模型：`shibing624/text2vec-base-chinese`（768 维真实中文 embedding）
- 服务方式：`scripts/local_model_server.py`（自实现 OpenAI 兼容端点，含 chat + embeddings）

| 验证项 | 实测结果 |
| --- | --- |
| 结构化输出探针（项目 provider + 校验/修复重试） | ✅ 一次通过，`attempts=1 tokens=520 repairs=0` |
| 端到端流水线（3 条 Golden Dataset 任务） | 1/3 通过，Retrieval Recall 100%，平均 450s/任务、73k–96k token/任务 |
| 失败原因（已定位并修复） | 0.5B 模型返回 schema 合法但内容为空的报告（无任何引用）→ 新增"空报告回退"到确定性抽取式写作器 |
| 修复后复测（同任务同模型） | ✅ 通过，**引用正确率 100%** |
| 检索消融（hash vs text2vec × dense/keyword/hybrid） | 见 [`docs/retrieval_ablation.md`](docs/retrieval_ablation.md)：hybrid 最优；真实 embedding 在本合成语料上并非自动更好 |

完整数据与复现命令见 [`docs/evaluation_local_model.md`](docs/evaluation_local_model.md) 与
[`docs/retrieval_ablation.md`](docs/retrieval_ablation.md)。要换成线上模型，只需
`export API_KEY=sk-... && python scripts/verify_live_model.py --limit 35`。

---

## Honesty Notes（关于数据与指标）

1. **没有虚构数字**：`docs/evaluation.md` 与 `docs/resume.md` 由脚本在运行后生成，所有指标可追溯到
   `benchmarks/*.json` 中的逐任务记录。
2. **离线 Web 语料是合成数据**：`data/web_corpus/*.jsonl` 是**内置合成示例语料**（元数据 `synthetic: true`），
   目的是让系统在无网络环境可完整运行；它不是真实搜索结果。真实检索请配置 `RESEARCHPILOT_WEB_SEARCH_MODE=http`
   与真实搜索 API。
3. **离线评测 vs 真实模型评测**：离线模式衡量工程管线（可在 CI 稳定回归）；真实模型模式衡量生成质量。
   两者的数字不可混用，报告中始终标注 provider。
4. **已知不足**：见 [Future Work](#future-work) 与 `docs/development_log.md` 的"仍存在的问题"章节。
5. **指标分母是显式声明的**：Recall / Context Relevance 只在声明了期望来源的任务上取平均，
   Citation Correctness 只在要求引用的任务上取平均，Tool Selection 只在声明期望工具的任务上取平均；
   没有期望值的任务不会贡献"真空 1.0"，`n/a` 不会被渲染成 `0%` 或 `100%`。
6. **状态语义诚实**：没有任何可用证据的运行会被标记为 `degraded`（而不是 `succeeded`），
   即使所有工具调用都返回 ok=True；只有至少产生一条可用证据且没有错误时才是 `succeeded`。
7. **Docker 未在本机执行**：本开发环境没有 docker 引擎（`docker` 不可用），因此
   `docker build` / `docker compose up` 未实机运行；已做的是静态一致性校验（compose YAML 解析、
   `COPY` 源文件存在性、镜像内命令与本机可运行的 uvicorn/MCP 命令一致、healthcheck 路径存在、
   以及 `pip install -e .` 的打包校验）。详见 `docs/development_log.md`。

---

## Future Work

- 用真实模型 + 真实搜索 API 跑一轮公开可复现的 benchmark，并把结果附在 `docs/evaluation.md`。
- 为检索增加交叉编码器重排与真实 embedding（`sentence-transformers`/BGE）并对比 Recall@k。
- 支持并行子任务执行（当前按计划顺序执行）与流式输出（SSE）。
- 把 Trace 导出为 OpenTelemetry，接入 Langfuse/Jaeger 等既有可观测栈。
- 引入人工标注的引用正确性抽样评估（现为代码判定 + 弱标注）。
- 多租户与鉴权（API Key、工具级 RBAC）、生产级限流与配额。
- 使用 `pgvector`/Qdrant 替换本地向量存储，支持增量索引与大规模语料。

---

## Repository Layout

```
researchpilot/            核心包（agents / rag / tools / mcp / memory / observability / evaluation / api）
  api/static/             零依赖前端（index.html + app.js + styles.css）
  mcp/                    MCP server + 三种传输客户端
  evaluation/             Golden Dataset 判分、指标、runner、Markdown 报告生成
data/knowledge_base/      示例知识库（10 篇、39 个 chunk）
data/web_corpus/          离线 Web 合成语料
eval/golden_dataset.jsonl 35 条评测任务
tests/{unit,integration,evaluation,fixtures}
docs/                     architecture / adr / api / evaluation / resume / audit_report / development_log
examples/                 示例运行结果（报告、result JSON、trace JSON）
scripts/                  run_benchmark.py / run_demo.py
```
