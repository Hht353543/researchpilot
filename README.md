# ResearchPilot

多 Agent 深度研究系统。给它一个研究问题，它会拆解任务、检索知识库和 Web、收集证据、交叉校验、必要时补检，最后写出一份结构化报告，报告里的每条结论都绑定可解析的来源。

```
LLM → Prompt → Structured Output → Tool Calling → RAG → Knowledge Base → MCP
    → Multi-Agent → Memory → Evaluation → Observability / Trace → FastAPI → Docker → CI
```

文档入口：

- 需求逐条对照：[`docs/requirements_traceability.md`](docs/requirements_traceability.md)
- 设计与决策： [`docs/architecture.md`](docs/architecture.md)、[`docs/adr/`](docs/adr/)
- 代码审查与修复记录：[`docs/audit_report.md`](docs/audit_report.md)、[`docs/development_log.md`](docs/development_log.md)
- 实测指标（脚本生成）：[`docs/evaluation.md`](docs/evaluation.md)、[`docs/resume.md`](docs/resume.md)

## 能力一览

| 模块 | 实现 |
| --- | --- |
| 多 Agent 编排 | Planner → Researcher → Verifier → Critic → Writer，结构化状态，可重试、可降级 |
| Agent 状态 | 全部走 Pydantic Schema：`ResearchPlan` / `EvidenceBundle` / `VerificationReport` / `CritiqueReport` / `FinalReport` |
| RAG 管线 | Loader → Cleaner → 标题感知分块 → Embedding → 向量库 → 混合检索 → 重排 → 上下文组装 |
| 检索模式 | 语义检索 / BM25 关键词 / RRF 混合 / 元数据过滤 / 查询改写 / IDF 重排（可选 LLM 重排） |
| 工具系统 | 统一 Tool Registry：Schema 校验、权限分级、超时、重试、缓存、Trace、调用预算 |
| 工具参数策略 | 每个工具自己声明怎么把子任务翻译成参数，Agent 里没有 `if tool_name == ...` 分发 |
| MCP | 自研 MCP Server（JSON-RPC 2.0），暴露 `search_knowledge` / `get_document` / `search_web` / `get_research_context`，支持 in-process、stdio、streamable-HTTP |
| Memory | 短期（token 预算滚动窗口）、工作记忆（计划、子任务、证据、观测）、长期记忆（TTL、去重、重要度、来源、命中次数持久化） |
| Evaluation | 35 条 Golden Dataset、12 类场景、代码判分、自动生成 `docs/evaluation.md` |
| Observability | 统一 Trace：Task → Agent → LLM / Tool / Retrieval / Retry，记录延迟、token、模型、输入输出、错误 |
| 服务化 | FastAPI 加零依赖前端：研究输入、模型参数、知识库、Agent 时间线、报告、指标、评测面板 |
| 知识库管理 | 入库、检索、删除（级联删 chunk）、全量重建索引 |
| LLM 抽象 | 任意 OpenAI 兼容端点（OpenAI / DeepSeek / vLLM / Ollama 等）加确定性离线 provider；真实 HTTP 链路由本地兼容端点端到端测试覆盖 |
| 工程质量 | 282 个测试（unit / integration / evaluation，含真实 HTTP provider 链路）、ruff、mypy、pip check、Dockerfile、docker-compose、GitHub Actions |

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

## Agent Workflow

1. PlannerAgent 把问题拆成 3–6 个子任务，为每个子任务挑工具并声明期望产出。输出要过一遍代码校验：未知工具剔除、缺工具回退、强制保留一个 synthesis 子任务。
2. ResearchAgent 按子任务执行工具（知识库检索、Web、MCP、文档、计算、元数据），记录观测，再把观测压成可引用的证据条目：claim、quote、source_id。
3. VerifierAgent 逐条复核证据：引用是否真实存在、quote 是否出自该来源、claim 是否被 quote 支撑、子任务覆盖度和来源质量。判定在代码里重新推导，不采信模型自评。
4. CriticAgent 找缺口和逻辑问题（覆盖度、引用、时效、冗余），必要时给出 follow-up 查询。
5. WriterAgent 只用通过校验的证据写作。引用由代码强制绑定：证据集里没有的 `[E#]` 会被剔除并记进 `dropped_citations`，参考文献也由代码从来源元数据生成。

`max_iterations`、工具调用预算、token 预算、工具超时和重试次数一起兜住循环，Agent 不会无限跑下去。

## RAG

- Loader：Markdown（含 YAML front matter）、TXT、JSON、JSONL、CSV。
- Cleaner：全角归一化、HTML 噪声和样板行剔除、重复行去重，并返回压缩统计。
- Chunker：标题感知分块，chunk 以章节路径开头（`文档 > 小节`），按句边界切分并带重叠窗口。这样标题信息也参与检索，引用 quote 不会从句子中间截断。
- Embedding：默认确定性哈希向量（离线可用），可切到 OpenAI 兼容 `/embeddings` 或 sentence-transformers。
- Vector Store：内存实现加 JSON 持久化，支持 dense、BM25 keyword、RRF hybrid 与元数据过滤。
- Retriever：查询改写 → 混合召回 → IDF 加权重排（可选 LLM 重排）→ 上下文组装，全程产生 Trace。
- Knowledge Base 字段：`document_id` / `title` / `source` / `chunk_id` / `content` / `metadata` / `created_at` / `embedding`。

检索质量由 Golden Dataset 量化，指标见下面的 Benchmark。

## Tool Calling

每个工具声明 `name` / `description` / `input schema` / `output schema` / `permission` / `timeout` / `retry policy`，由 Registry 统一路由：

| tool | 作用 | 权限 |
| --- | --- | --- |
| `knowledge_search` | 混合检索知识库（支持过滤、改写、重排） | read_only |
| `web_search` | Web 检索（离线语料或真实 HTTP 后端） | network |
| `document_reader` | 按 doc_id / chunk_id 读取原文 | read_only |
| `calculator` | AST 白名单安全表达式求值 | compute |
| `metadata` | 文档与分块元数据、主题、时间、任务上下文 | read_only |
| `mcp_research_context` | 通过 MCP Server 获取打包好的研究上下文 | network |

每次调用产生一条 Trace span：`{tool, arguments, result, latency_ms, success}`。

## MCP

`researchpilot/mcp/` 实现了 initialize、tools/list、tools/call 三个协议面，以及三种传输：

```bash
# stdio（Claude Desktop、Cursor 这类客户端可以直接接）
python -m researchpilot.mcp_server --stdio

# streamable HTTP（可以独立部署、单独扩缩容）
RESEARCHPILOT_MCP_TRANSPORT=http python -m researchpilot.mcp_server

# in-process（默认，用于单进程部署和测试）
```

Agent 通过同一个 Tool Registry 使用 MCP 能力。服务端只管工具定义、实现、权限和超时，客户端只管协议交互，两边不互相依赖。

## Memory

| 层 | 作用 | 关键机制 |
| --- | --- | --- |
| Short-Term | 当前任务上下文 | token 预算、滚动淘汰、查询去重 |
| Working | 当前研究任务状态（plan / subtasks / evidence / observations） | 证据去重（hash + Jaccard）、缺口计算、快照 |
| Long-Term | 跨任务偏好、主题、结论、事实 | TTL、相似度去重、重要度加时间衰减加命中次数排序、来源与时间戳 |

## Evaluation

`eval/golden_dataset.jsonl` 有 35 条测试任务，覆盖 12 类场景：`simple_fact`、`multi_step`、`rag`、`tool_calling`、`mcp`、`multi_agent`、`citation`、`prompt_injection`、`tool_abuse`、`timeout_recovery`、`no_result`、`bad_source`。

判分全部由代码完成，不用 LLM 当裁判：报告是否存在、关键词覆盖、禁用内容、引用可解析率、检索召回、上下文相关性、缺口是否显式声明、故障处理是否符合预期、状态与延迟预算。

```bash
python scripts/run_benchmark.py --provider mock      # 离线确定性，CI 用这个
python scripts/run_benchmark.py --provider openai    # 真实模型（需要 API_KEY）
```

真实模型可以用一条命令自检并跑评测（脚本不打印密钥，凭据缺失或被拒绝时给出明确退出码）：

```bash
export API_KEY=sk-...                                # 或 RESEARCHPILOT_API_KEY
python scripts/verify_live_model.py --probe-only     # 1 次最小真实调用
python scripts/verify_live_model.py --limit 35       # 真实模型跑完整 Golden Dataset
```

这个开发环境里的 `OPENAI_API_KEY` 对 `api.openai.com` 返回 HTTP 401 `invalid_api_key`，所以仓库里提交的评测数字来自离线确定性 provider。换上有效凭据后跑上面两条命令即可生成真实模型数字，`docs/evaluation.md` 和 `docs/resume.md` 会自动重写。

指标包括 Task Success Rate、Retrieval Recall、Context Relevance、Citation Correctness、Tool Selection Accuracy 与 F1、Tool Success Rate、延迟（P50/P95）、token 和成本，全部来自真实运行并写入 `docs/evaluation.md`。

## Observability

```
Task
 ├── Agent（PlannerAgent / ResearchAgent / ...）
 │    ├── LLM（prompt/completion、model、tokens、latency）
 │    ├── Tool（名称、参数、结果条数、成功与否、重试次数）
 │    ├── Retrieval（策略、改写后的查询、命中 chunk、doc_ids）
 │    └── Retry（失败原因）
 └── Final Result
```

Trace 落到 `runs/{task_id}.trace.json`，通过 `GET /research/{task_id}/trace` 暴露，前端按时间线渲染。评测指标也从 Trace 里算，比如工具选择和检索命中的 doc_ids。

## Installation

```bash
# 安装（Python 3.11+；默认哈希向量和 mock provider 都能离线跑）
pip install -e ".[dev]"

# 建知识库索引，跑一次研究
python -m researchpilot.cli ingest
python -m researchpilot.cli research "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。"

# 起服务，前端在 http://127.0.0.1:8000/
python -m researchpilot.cli serve
```

## Configuration

所有配置都可以走环境变量或 `.env`，字段清单见 `.env.example`。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RESEARCHPILOT_PROVIDER` | `mock`（离线确定性）或 `openai`（任意 OpenAI 兼容端点） | `mock` |
| `MODEL` / `API_KEY` / `BASE_URL` | 模型、密钥、端点；DeepSeek、vLLM、Ollama 等兼容端点同样适用 | `gpt-4o-mini` / 空 / 官方地址 |
| `TEMPERATURE` / `PRESENCE_PENALTY` / `FREQUENCY_PENALTY` / `MAX_TOKENS` | 生成参数，前四项前端可覆盖 | 0.2 / 0 / 0 / 1200 |
| `RESEARCHPILOT_TOP_K` / `RETRIEVE_K` | 最终上下文条数 / 召回候选数 | 6 / 12 |
| `RESEARCHPILOT_MAX_ITERATIONS` / `TOKEN_BUDGET` | 研究迭代上限 / 单任务 token 预算 | 2 / 80000 |
| `RESEARCHPILOT_EMBEDDING_PROVIDER` / `EMBEDDING_DIM` | `hash` 或 `openai`，向量维度 | hash / 384 |
| `RESEARCHPILOT_MCP_TRANSPORT` / `MCP_URL` | `inprocess` / `stdio` / `http` | inprocess |
| `RESEARCHPILOT_WEB_SEARCH_MODE` / `WEB_SEARCH_URL` | `offline`（内置语料）或 `http`（真实搜索 API） | offline |
| `RESEARCHPILOT_KB_PATH` / `RUNS_PATH` | 知识库目录 / 运行产物目录 | `data/knowledge_base` / `runs` |

## API

完整参考见 [`docs/api.md`](docs/api.md)。核心端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/research` | 运行研究，`mode=sync` 直接返回结果，`mode=async` 返回 202 和 `task_id` |
| GET | `/research/{task_id}` | 完整结果（计划、证据、校验、报告、指标） |
| GET | `/research/{task_id}/trace` | Agent / LLM / Tool / Retrieval / Retry 轨迹 |
| GET | `/research/{task_id}/sources` | 全部引用来源 |
| GET | `/research/{task_id}/metrics` | 延迟、token、工具调用、重试 |
| GET | `/kb/documents`、`/kb/search`、`/kb/documents/{doc_id}` | 知识库浏览与检索 |
| POST | `/kb/documents`、`/kb/reindex` | 文档入库与重建索引 |
| DELETE | `/kb/documents/{doc_id}` | 删除文档及其全部 chunk |
| GET | `/mcp/tools`、POST `/mcp/call` | MCP 工具发现与调用 |
| GET | `/evaluation/latest` | 最近一次 benchmark 指标 |

## Docker

`docker-compose.yml` 把 API 和 MCP Server 起成两个独立服务，API 通过 streamable-HTTP 调用 MCP：

```bash
docker compose up --build
# API + 前端: http://localhost:8000/
# MCP Server: http://localhost:8765/health
```

这台开发机上没有可用的 Docker 引擎（`docker`、`podman`、`buildah`、`nerdctl` 都不存在，WSL 也没装发行版），所以本机从未执行过 `docker build` 或 `docker compose up`。容器验收放在 CI 里跑，用的是 `ubuntu-latest` 的 Linux runner：

| 步骤 | 命令 |
| --- | --- |
| 引擎自检 | `docker version`、`docker compose version`（无引擎直接失败，不会跳过） |
| 构建 | `docker build --file Dockerfile --tag researchpilot:latest .` |
| 校验 compose | `docker compose --file docker-compose.yml config --quiet` |
| 启动 | `docker compose up --detach --wait --wait-timeout 300` |
| 健康检查 | `docker inspect '{{.State.Health.Status}}'`，mcp 和 api 各一次 |
| 主机侧验证 | `python3 scripts/container_smoke.py --api-url ... --mcp-url ...` |
| 容器内 smoke | `docker compose exec -T api python scripts/container_smoke.py --in-container` |
| 失败取证 | `docker compose logs`（`if: failure()`） |
| 清理 | `docker compose down --volumes --remove-orphans`（`if: always()`） |

这个 job 里没有 `if:`、没有 `continue-on-error`、没有假的引擎，容器坏了整个 workflow 就会红。`tests/unit/test_ci_docker_job.py` 把这些性质固定成回归测试，防止以后被改掉。

2026-09-14 的运行（[run 34820524987](https://github.com/Hht353543/researchpilot/actions/runs/34820524987)）里 docker job 通过，实测结果：镜像构建成功，`researchpilot-mcp-1` 和 `researchpilot-api-1` 都 healthy，主机侧和容器内两轮 smoke 全过，API `/health` 报 `mcp_transport=http`，前端 200，一次研究任务 `succeeded`、`mcp_calls=1`、8 条证据、8 个来源。

本机还能做的等价验证：

```bash
python scripts/compose_smoke.py      # 也可由 pytest 执行：tests/integration/test_compose_topology.py
# 容器已经在跑时（CI 的 docker job 就是这么调的）：
python scripts/container_smoke.py --api-url http://127.0.0.1:8000 --mcp-url http://127.0.0.1:8765
# 容器内自检（镜像内容、卷可写、端到端研究任务）：
python scripts/container_smoke.py --in-container
```

`scripts/compose_smoke.py` 读真实的 `docker-compose.yml`：解析服务、`command`、`ports`、`environment` 的 `${VAR:-default}` 插值、`depends_on: service_healthy`，按依赖顺序先起 `mcp` 等健康、再起 `api`，然后断言 MCP `/health`、API `/health` 里的 `mcp_transport=http`、`/mcp/tools` 走 HTTP、前端 200，以及一次研究任务确实通过 HTTP 调用了 MCP。容器内路径和主机名（`/app/...`、`mcp:8765`）到宿主机的映射会逐条打印出来。

`scripts/container_smoke.py` 只用标准库（`urllib`），所以既能被 CI runner 调用，也能在容器里跑。它检查 MCP 和 API 的 `/health`、MCP JSON-RPC（含中文 `tools/call`）、`/mcp/tools` 是否走 HTTP、前端 200，以及一次真实研究任务的状态、`mcp_calls`、证据、来源和报告；任何一项不满足就以非 0 退出。它的失败路径（服务不可达、镜像缺数据）由 `tests/integration/test_container_smoke.py` 用真实启动的双服务验证过。

## Testing

```bash
python -m pytest -q                       # 单元 + 集成 + 评测（282 个测试）
python -m pytest -q -m "not evaluation"   # 快速回归
python -m pytest -q -m evaluation         # 全量 Golden Dataset 冒烟
ruff check . && ruff format --check . && mypy researchpilot
python -m pip check                       # 依赖一致性
python scripts/ci_dry_run.py              # 在本地逐条执行 CI 工作流里的命令
python scripts/verify_fresh_clone.py      # 从干净 clone 复现上述流程
```

测试目录：`tests/unit/`（组件契约、依赖一致性、路径解析、工具参数策略、安全加固）、`tests/integration/`（流水线、API、MCP 三种传输、OpenAI 兼容 HTTP provider 全链路）、`tests/evaluation/`（数据集完整性、评测器机制、全量冒烟）、`tests/fixtures/`（样例语料、mock 数据和本地 OpenAI 兼容端点）。

前端是零依赖 vanilla JS，没有 npm 和构建步骤，也就没有 `npm install`。逻辑仍然被测：`tests/frontend/app.test.mjs` 用 `node --test` 在 VM 加 DOM stub 里加载 `app.js`，覆盖 Markdown 渲染、Agent 时间线、指标卡片、知识库列表、来源与评测面板（含 HTML 转义和空状态）；`tests/unit/test_config_and_paths.py` 额外做前端与 OpenAPI 的契约测试，前端调用的每个 URL 都必须存在于路由表，Settings 字段和面板元素必须存在并被读取。CI 里是独立步骤：`node --test "tests/frontend/**/*.test.mjs"`。

## Benchmark

最近一次离线评测（`provider=mock`，35 条任务），原始数据在 `benchmarks/latest_mock.json`，报告见 [`docs/evaluation.md`](docs/evaluation.md)：

| 指标 | 结果 |
| --- | --- |
| Task Success Rate | 100%（35/35） |
| Retrieval Recall@6 | 93.9%（分母 30 个含期望来源的任务） |
| Context Relevance | 28.8%（同一分母） |
| Citation Correctness | 100%（分母 34 个要求引用的任务） |
| Tool Selection Accuracy / F1 | 100% / 85.9%（分母 35 个含期望工具的任务） |
| Tool Success Rate | 74.6%（130 次调用，含主动注入的超时、失败和预算任务） |
| Avg / P95 Latency | 0.32s / 1.38s |
| Tokens / Cost | 639,511 / $0.000000 |

离线 `mock` provider 是确定性脚本模型，用来验证工程管线：检索、工具、校验、引用绑定、追踪、评测、故障恢复。这组数字衡量的是管线正确性和回归基线，不代表真实模型的生成质量。真实模型质量用 `python scripts/run_benchmark.py --provider openai` 重新跑，数字会自动覆盖到 `docs/evaluation.md`。

## 真实模型验证

除了离线 provider，这个仓库也用本地真实模型跑通过整条链路，不需要厂商 Key 和外网：

- 生成模型：`Qwen/Qwen1.5-0.5B-Chat`（真实权重，GPU）
- 向量模型：`shibing624/text2vec-base-chinese`（768 维中文 embedding）
- 服务方式：`scripts/local_model_server.py`，自实现 OpenAI 兼容端点，含 chat 和 embeddings

| 验证项 | 结果 |
| --- | --- |
| 结构化输出探针（项目 provider 加校验修复重试） | 一次通过，`attempts=1 tokens=520 repairs=0` |
| 端到端流水线（3 条 Golden Dataset 任务） | 1/3 通过，Retrieval Recall 100%，平均 450s/任务，73k–96k token/任务 |
| 失败原因 | 0.5B 模型返回 schema 合法但内容为空的报告，没有任何引用 |
| 修复方式 | 新增空报告回退：有可用证据但报告无引用时，改用确定性抽取式写作器 |
| 修复后复测（同任务同模型） | 通过，引用正确率 100% |
| 检索消融（hash 与 text2vec × dense/keyword/hybrid） | 见 [`docs/retrieval_ablation.md`](docs/retrieval_ablation.md)，hybrid 最好；真实 embedding 在这份合成语料上不是自动更好 |

完整数据和复现命令见 [`docs/evaluation_local_model.md`](docs/evaluation_local_model.md)。

## 数据说明

- `docs/evaluation.md` 和 `docs/resume.md` 由脚本在运行后生成，每个指标都能追到 `benchmarks/*.json` 里的逐任务记录。
- `data/web_corpus/*.jsonl` 是内置的合成语料（元数据里标了 `synthetic: true`），目的是让系统在没有网络时也能完整跑通，它不是真实搜索结果。要用真实检索，配置 `RESEARCHPILOT_WEB_SEARCH_MODE=http` 和真实搜索 API。
- 离线模式衡量工程管线，可以稳定回归；真实模型模式衡量生成质量。两组数字不能混用，报告里都会标 provider。
- 指标分母是显式声明的：Recall 和 Context Relevance 只在声明了期望来源的任务上取平均，Citation Correctness 只在要求引用的任务上取平均，Tool Selection 只在声明期望工具的任务上取平均。没有期望值的任务贡献 `n/a`，不会被渲染成 0% 或 100%。
- 没有任何可用证据的运行标记为 `degraded`，即使所有工具调用都返回成功；只有至少产生一条可用证据且没有错误才是 `succeeded`。
- 这台机器没有 Docker 引擎，容器验证由 CI 承担，见上面的 Docker 一节。

## Future Work

- 用真实模型加真实搜索 API 跑一轮可复现的 benchmark，结果附到 `docs/evaluation.md`。
- 检索增加交叉编码器重排和真实 embedding（sentence-transformers、BGE），对比 Recall@k。
- 子任务并行执行（现在按计划顺序跑）和流式输出（SSE）。
- Trace 导出为 OpenTelemetry，接入 Langfuse、Jaeger 这类既有可观测栈。
- 人工标注的引用正确性抽样评估（现在是代码判定加弱标注）。
- 多租户与鉴权（API Key、工具级 RBAC）、限流与配额。
- 用 `pgvector` 或 Qdrant 替换本地向量存储，支持增量索引和更大语料。

## Repository Layout

```
researchpilot/            核心包（agents / rag / tools / mcp / memory / observability / evaluation / api）
  api/static/             零依赖前端（index.html + app.js + styles.css）
  mcp/                    MCP server + 三种传输客户端
  evaluation/             Golden Dataset 判分、指标、runner、Markdown 报告生成
data/knowledge_base/      示例知识库（10 篇，39 个 chunk）
data/web_corpus/          离线 Web 合成语料
eval/golden_dataset.jsonl 35 条评测任务
tests/{unit,integration,evaluation,fixtures}
docs/                     architecture / adr / api / evaluation / resume / audit_report / development_log
examples/                 示例运行结果（报告、result JSON、trace JSON）
scripts/                  run_benchmark.py / run_demo.py
```
