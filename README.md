# ResearchPilot

多 Agent 深度研究系统。给它一个研究问题，它会拆解任务、检索知识库和 Web、收集证据、交叉校验、必要时补检，最后写出一份结构化报告，报告里的每条结论都绑定可解析的来源。

```
LLM → Prompt → Structured Output → Tool Calling → RAG → Knowledge Base → MCP
    → Multi-Agent → Memory → Evaluation → Observability / Trace → FastAPI → Docker → CI
```

文档入口：

- 需求逐条对照：[`docs/requirements_traceability.md`](docs/requirements_traceability.md)
- 上手使用：[`docs/usage.md`](docs/usage.md)
- 面试要点与实现方法：[`docs/interview_notes.md`](docs/interview_notes.md)
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
| 工程质量 | 401 个 pytest + 10 个前端测试（含真实 HTTP provider 链路）、ruff、mypy、pip check、Dockerfile、docker-compose、GitHub Actions |

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

## Recommended Development Setup

```bash
# 1. 获取代码并创建不继承全局包的 Python 3.11+ 环境
git clone <repository-url> researchpilot
cd researchpilot
python -m venv .venv

# 2. 激活环境（Windows PowerShell）
.\.venv\Scripts\Activate.ps1
# macOS / Linux 使用：source .venv/bin/activate

# 3. 按团队验证过的核心版本安装运行与开发依赖
python -m pip install -c constraints.txt -e ".[dev]"
python -m pip check

# 4. 验证代码基线
python -m pytest -q

# 5. 建索引并用默认 mock provider 完成一次离线研究
researchpilot ingest
researchpilot research "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。"

# 6. 启动 API 与前端：http://127.0.0.1:8000/
researchpilot serve
```

这是本地开发的唯一推荐路径。`constraints.txt` 只固定经过验证的 FastAPI、Starlette、Pydantic、
Uvicorn 和 HTTPX 核心组合；依赖声明仍以 `pyproject.toml` 为准。真实模型、可选 embedding 和手动
MCP 传输配置属于高级用法，基础安装不需要 API Key 或外部服务。

## Configuration

所有配置都可以走环境变量或 `.env`，字段清单见 `.env.example`。

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `RESEARCHPILOT_PROVIDER` | `mock`（离线确定性）或 `openai`（任意 OpenAI 兼容端点） | `mock` |
| `RESEARCHPILOT_BIND_HOST` / `API_PORT` | API 对宿主机的监听地址 / 端口 | `127.0.0.1` / `8000` |
| `RESEARCHPILOT_ALLOW_REMOTE_ACCESS` | 是否明确允许非回环地址；开启时必须同时配置访问 token | `false` |
| `RESEARCHPILOT_ACCESS_TOKEN` | API Bearer token；为空时仅适合本机开发 | 空 |
| `RESEARCHPILOT_MAX_REQUEST_BODY_BYTES` / `MAX_CONCURRENT_TASKS` | HTTP 请求体上限 / 单进程并发研究任务上限 | 1048576 / 4 |
| `RESEARCHPILOT_RESEARCH_TASK_TIMEOUT_S` | 单个研究任务的 wall-clock deadline | 300 |
| `RESEARCHPILOT_SHUTDOWN_TIMEOUT_S` | 关闭时等待受管 worker 清理的上限 | 5 |
| `RESEARCHPILOT_TASK_HEARTBEAT_INTERVAL_S` / `RECOVERY_STALE_AFTER_S` | 持久化任务心跳间隔 / 活跃 owner 租约失效窗口 | 5 / 30 |
| `MODEL` / `API_KEY` / `BASE_URL` | 模型、密钥、端点；DeepSeek、vLLM、Ollama 等兼容端点同样适用 | `gpt-4o-mini` / 空 / 官方地址 |
| `TEMPERATURE` / `PRESENCE_PENALTY` / `FREQUENCY_PENALTY` / `MAX_TOKENS` | 生成参数，前四项前端可覆盖 | 0.2 / 0 / 0 / 1200 |
| `RESEARCHPILOT_TOP_K` / `RETRIEVE_K` | 最终上下文条数 / 召回候选数 | 6 / 12 |
| `RESEARCHPILOT_MAX_ITERATIONS` / `TOKEN_BUDGET` | 研究迭代上限 / 单任务 token 预算 | 2 / 80000 |
| `RESEARCHPILOT_EMBEDDING_PROVIDER` / `EMBEDDING_DIM` | `hash` 或 `openai`，向量维度 | hash / 384 |
| `RESEARCHPILOT_MCP_TRANSPORT` / `MCP_URL` | `inprocess` / `stdio` / `http` | inprocess |
| `RESEARCHPILOT_WEB_SEARCH_MODE` / `WEB_SEARCH_URL` | `offline`（内置语料）或 `http`（真实搜索 API） | offline |
| `RESEARCHPILOT_KB_PATH` / `RUNS_PATH` | 知识库目录 / 运行产物目录 | `data/knowledge_base` / `runs` |

### Local development 与 shared deployment

本地开发是默认模式：CLI 和 Compose 都只发布到 `127.0.0.1`，访问 token 可以留空。若要让 API
监听非回环地址，必须同时显式打开远程访问并配置至少 16 个字符的 token，否则配置校验会拒绝启动：

```bash
export RESEARCHPILOT_BIND_HOST=0.0.0.0
export RESEARCHPILOT_ALLOW_REMOTE_ACCESS=true
export RESEARCHPILOT_ACCESS_TOKEN='replace-with-a-long-random-token'
python -m researchpilot.cli serve

curl -H "Authorization: Bearer $RESEARCHPILOT_ACCESS_TOKEN" http://server:8000/config
```

启用 token 后，`/health`、首页和静态文件保持公开，其余 API 都要求 `Authorization: Bearer ...`。
页面的 Model Settings 中可以输入 token；它只保存在当前浏览器标签页的 `sessionStorage`。这是一层最小共享部署保护，
不替代 TLS、反向代理、用户系统或细粒度权限。

单个请求体默认限制为 1 MiB，文档正文最多 500,000 字符，单进程最多同时执行 4 个研究任务；超限分别返回
`413 request_too_large` 和 `429 capacity_exceeded`。单任务仍受问题长度、最多 4 次研究迭代、token 预算、
模型请求超时、工具超时、有限重试和默认 300 秒任务级 wall-clock deadline 约束。异步任务可通过
`DELETE /research/{task_id}` 取消；取消与超时都会停止调度新的模型和工具调用，并释放逻辑容量槽。

长期记忆与知识库索引继续使用 JSON 持久化。写入通过同目录 SQLite 锁文件在 Windows/Linux
进程之间串行化，并在锁内重读最新快照、合并本次修改，再用同目录临时文件原子替换。知识库查询会检查
持久化文件签名；API 或独立 MCP 进程提交变更后，其他长驻进程会在下一次查询时自动重载索引，无需重启。
不同 memory 记录和不同知识文档的修改会合并；同一 memory logical record 沿用去重规则，累计命中次数，
且只有同等或更高 importance 的后续提交会替换内容。同一知识文档 ID 的并发更新采用最后提交版本生效。
持久化采用 persist-before-publish：只有原子文件替换成功后才发布新的内存状态。写入失败会回滚候选状态并向
调用方返回明确失败，`/health` 同时标记 degraded；损坏或结构无效的 JSON 会报告 corruption，原文件保持不变。

## API

完整参考见 [`docs/api.md`](docs/api.md)。核心端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/research` | 运行研究，`mode=sync` 直接返回结果，`mode=async` 返回 202 和 `task_id` |
| GET | `/research/{task_id}` | 完整结果（计划、证据、校验、报告、指标） |
| DELETE | `/research/{task_id}` | 协作式取消 pending/running 任务 |
| GET | `/research/{task_id}/trace` | Agent / LLM / Tool / Retrieval / Retry 轨迹 |
| GET | `/research/{task_id}/sources` | 全部引用来源 |
| GET | `/research/{task_id}/metrics` | 延迟、token、工具调用、重试 |
| GET | `/kb/documents`、`/kb/search`、`/kb/documents/{doc_id}` | 知识库浏览与检索 |
| POST | `/kb/documents`、`/kb/reindex` | 文档入库与重建索引 |
| DELETE | `/kb/documents/{doc_id}` | 删除文档及其全部 chunk |
| GET | `/mcp/tools`、POST `/mcp/call` | MCP 工具发现与调用 |
| GET | `/evaluation/latest` | 最近一次 benchmark 指标 |

## Recommended Team Deployment

`docker-compose.yml` 把 API 和 MCP Server 起成两个独立服务，API 通过 streamable-HTTP 调用 MCP：

```bash
docker compose up --build
# API + 前端: http://localhost:8000/
# MCP Server: http://localhost:8765/health
```

Compose 的 API 和 MCP 端口默认都绑定宿主机回环地址。共享发布 API 时使用上面的三个
`RESEARCHPILOT_*` 变量；MCP 端口仍只发布到宿主机回环地址，API 在 Compose 内部网络访问它。

这台开发机已安装 Docker CLI 与 Compose，但 Docker daemon 当前不可用，因此本轮只能执行 Compose 配置解析和无引擎拓扑 smoke，不能在本机执行 `docker build` 或 `docker compose up`。真实容器验收由 CI 的 `ubuntu-latest` Linux runner 执行：

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
python -m pytest -q                       # 单元 + 集成 + 评测（401 个测试）
python -m pytest -q -m "not evaluation"   # 快速回归
python -m pytest -q -m evaluation         # 全量 Golden Dataset 冒烟
ruff check . && ruff format --check . && mypy researchpilot
python -m pip check                       # 依赖一致性
python scripts/ci_dry_run.py              # 在本地逐条执行 CI 工作流里的命令
python scripts/verify_fresh_clone.py      # 从干净 clone 复现上述流程
```

测试目录：`tests/unit/`（组件契约、依赖一致性、路径解析、工具参数策略、安全加固）、`tests/integration/`（流水线、API、MCP 三种传输、OpenAI 兼容 HTTP provider 全链路）、`tests/evaluation/`（数据集完整性、评测器机制、全量冒烟）、`tests/fixtures/`（样例语料、mock 数据和本地 OpenAI 兼容端点）。

前端是零依赖 vanilla JS，没有 npm 和构建步骤，也就没有 `npm install`。逻辑仍然被测：`tests/frontend/app.test.mjs` 用 `node --test` 在 VM 加 DOM stub 里加载 `app.js`，覆盖 Markdown 渲染、Agent 时间线、指标卡片、知识库列表、来源与评测面板（含 HTML 转义和空状态）；`tests/unit/test_config_and_paths.py` 额外做前端与 OpenAPI 的契约测试，前端调用的每个 URL 都必须存在于路由表，Settings 字段和面板元素必须存在并被读取。CI 里是独立步骤：`node --test "tests/frontend/**/*.test.mjs"`。

### Reproducible package build

开发依赖已经包含 `build` 与 `wheel`。在推荐的 `.venv` 中执行：

```bash
python -m build                         # 生成 dist/*.tar.gz 与 dist/*.whl
python -m venv .venv-wheel              # 第二个空环境，不使用 editable install
.venv-wheel/bin/python -m pip install -c constraints.txt dist/*.whl
.venv-wheel/bin/python scripts/installed_package_smoke.py
```

Windows 将上面的 `.venv-wheel/bin/python` 替换为 `.venv-wheel\Scripts\python.exe`。smoke 脚本验证包导入、
CLI `--help`、配置加载、API 初始化、MCP 工具注册和一条离线研究路径。CI 的 `package` job 执行同一流程。

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

### 真实模型对照（deepseek-chat，2026-09-15）

同一套用例、同一套判分，真实模型跑过两轮：修复前，以及修掉「结构化输出契约、状态语义、写作器引用回收」
三处缺陷之后。两轮结果都提交在仓库里，逐任务原始数据在 `benchmarks/`。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| Task Success | 25/35 (71.4%) | 24/35 (68.6%) |
| Recall@6 / Context Relevance | 91.7% / 28.8% | 90.6% / 9.2% |
| Citation Correctness | 90.3% | 72.7% |
| Tool Success | 74.6% | 88.1% |
| 回退写作器产出 | 24/35 | 17/35 |
| 状态分布 | degraded 28 / failed 7 | degraded 28 / succeeded 7 |
| 平均延迟 / 成本 | 111.1s / $1.16 | 184.6s / $1.75 |

修复后分数没有变高，这是预期内的：修复前 35/35 任务的计划走了确定性回退、24/35 的报告由回退写作器产出，
分数里有相当一部分不属于模型。修完之后管线第一次真正使用模型自己的计划与报告，指标也随之落到模型的真实水平，
瓶颈因此可以定位到模型的计划质量与引用纪律，而不是管线主体。完整报告见
[`docs/evaluation_live_deepseek.md`](docs/evaluation_live_deepseek.md) 与
[`docs/resume_live_deepseek.md`](docs/resume_live_deepseek.md)。

注意：上表"修复后"一列跑在**写作器字段别名修复之前**。那次运行里模型的报告用
`findings[].narrative` 命名章节，而 schema 要求 `sections[].body`，未知字段被静默丢弃，
报告因此被判为空、改由抽取式回退写作器产出——引用数字既被回退链抬高、也被它掩盖。
在受影响的 9 条引用失败任务上重测（别名修复后，分两批：3 条 + 6 条）：`writer_fallback_tasks`
从 9/9 降到 **0/9**，引用正确率从 0% 升到 **100%**，8/9 通过（余下 1 条只差一项关键词覆盖，
与引用无关），报告全部由模型自己撰写。
原始数据：`benchmarks/live_deepseek_citation_verify_10tasks.json`（暴露问题的那一次）与
`benchmarks/live_deepseek_alias_verify_{3,6}tasks.json`（别名修复后）。

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
- `status` 只表示生命周期；`completed` 的结果再用 `quality=succeeded|degraded` 表示质量。没有证据的完成任务质量为 `degraded`。
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
