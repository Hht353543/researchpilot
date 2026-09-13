# Code Review & Bug-Fix Audit Report

本文件是「ResearchPilot 全量代码审查 + Bug 排查 + 缺失功能检查 + 运行验证 + 修复」的完整交付记录。
所有结论都对应可复现的证据（命令输出、测试、trace、日志、文件），不含推测或美化。

审查范围：仓库全部代码与配置（backend / frontend / agents / tools / rag / memory / mcp / evaluation /
tests / scripts / docs / config / docker / CI），依赖状态（`pip check`、干净环境安装），
以及真实运行（离线 provider、真实 HTTP OpenAI 兼容端点、MCP 双服务链路）。

---

## A–J：项目能力清单

### A. 当前项目模块 / B. 每个模块的作用 / C. 依赖关系

| 模块 | 作用 | 依赖 |
| --- | --- | --- |
| `researchpilot/config.py` | 全部配置（env / .env），路径解析、价格表 | utils |
| `researchpilot/schemas.py` | 全系统状态 Schema（Plan / Evidence / Verification / Critique / Report / Trace / Metrics） | pydantic |
| `researchpilot/llm/` | Provider 抽象（mock / OpenAI 兼容）、结构化输出运行器（校验+修复重试+预算）、Prompt 模板 | config, schemas, observability |
| `researchpilot/rag/` | Loader → Cleaner → Chunker → Embedding → VectorStore → Retriever（hybrid/rewrite/rerank）→ KnowledgeBase | config, llm(重排/改写), utils |
| `researchpilot/tools/` | 工具契约（schema/permission/timeout/retry/参数策略）、Registry、6 个内置工具、Web 后端 | rag, mcp, observability, security |
| `researchpilot/mcp/` | MCP Server（JSON-RPC 2.0）+ 3 种传输 + 客户端 | rag, tools.web_backend |
| `researchpilot/memory/` | 短期 / 工作 / 长期记忆（TTL、去重、重要度、来源、命中持久化） | schemas, config |
| `researchpilot/observability/` | Tracer（span 树 + 环境 ContextVar）、TraceStore、成本估算 | schemas, config |
| `researchpilot/agents/` | Planner / Researcher / Verifier / Critic / Writer + 共享 Runtime | llm, tools, rag, memory, observability, sources |
| `researchpilot/pipeline.py` | 编排、迭代控制、预算、降级、状态判定、持久化 | agents, store, observability |
| `researchpilot/store.py` | 结果持久化（`runs/*.result.json`） | schemas |
| `researchpilot/api/` | FastAPI 服务（14 组端点）+ ServiceContainer + 零依赖前端 | pipeline, mcp, rag |
| `researchpilot/evaluation/` | Golden Dataset 判分、指标聚合、runner、Markdown 报告生成、故障注入 | pipeline, rag, llm |
| `researchpilot/security.py` | 提示注入 / 工具滥用特征检测 | utils |
| `researchpilot/cli.py` | `research` / `serve` / `ingest` / `mcp` / `bench` | 全部 |
| `tests/` | unit / integration / evaluation + fixtures（含本地 OpenAI 兼容端点） | 全部 |
| `scripts/` | `run_benchmark.py`、`run_demo.py` | evaluation, pipeline |

依赖方向（单向，无循环）：

```
api / cli / scripts
        ↓
     pipeline ──► agents ──► llm / tools / rag / memory / sources
        ↓                        ↓        ↓
     store                 mcp.server   security
        ↓
  observability  ◄── 所有模块（Tracer 由 runtime 注入）
```

### D. 已实现功能（均被真实调用链覆盖）

结构化多 Agent 工作流；迭代补检；证据抽取与引用绑定；代码化校验（引用存在性/归属性/相关性/来源质量）；
混合检索（dense + BM25 + RRF）、元数据过滤、查询改写、IDF/LLM 重排；工具注册表（权限/超时/重试/预算/缓存/Trace）；
MCP Server 与 3 种传输；三层记忆；统一 Trace；35 条 Golden Dataset 与 9 类指标；FastAPI（含 KB 增删查、
重建索引、MCP 桥接、评测摘要）；前端 7 个面板；CLI；Docker/Compose/CI 配置；138+ 测试。

### E. 部分实现功能（明确的能力边界）

| 能力 | 现状 | 原因 |
| --- | --- | --- |
| 检索语义质量 | Recall 93.9% 但 Context Relevance 28.8% | 默认哈希向量，无语义泛化；需真实 embedding + 交叉编码器重排 |
| 并行执行 | 子任务串行 | 为保证可测试性与确定性；并行化列入 Future Work |
| 工具超时 | 超时后返回失败，但后台线程不可强制中断 | `ThreadPoolExecutor` 限制；生产建议异步客户端/子进程隔离 |
| 语义类判分 | 代码规则判分，无 LLM judge | 可复现性优先；LLM judge 保留扩展位但默认关闭 |
| 持久化 | JSON 单机存储 | 演示与单机足够；生产需数据库/对象存储 |

### F. Stub / TODO / FIXME / 空实现

- `TODO` / `FIXME` / `XXX` / `HACK` / `NotImplementedError` / `stub`：**0 处**（`rg` 全仓扫描）。
- `placeholder`：2 类，均为有意设计 —— 异步提交时写入的 `pending` 占位结果、前端输入框 `placeholder` 文案。
- `pass` 共 8 处：测试辅助、异常兜底注释块、MCP 通知（协议规定无响应），无空实现。
- 空实现 / 未实现抽象方法：无（`BaseTool.run`、`LLMProvider.complete` 均有具体子类实现）。
- `MockLLMProvider` 不是 stub：它是可运行的确定性离线 provider，用于 CI 与离线评测，并在所有报告中标注。

### G. 可疑实现（已逐条处置）

初始扫描中标记的可疑项与处置见下方「发现分类」；全部 25 项均有明确结论（修复 / 删除 / 接入调用链 / 文档说明）。

### H. 未被调用代码（已处置）

| 符号 | 处置 |
| --- | --- |
| `SourceRegistry._by_doc`（只写不读） | 增加 `source_for_doc()` 并接入文档读取路径 |
| `tracer_scope` / `current_tracer`（仅测试使用） | 流水线用 `tracer_scope` 设置环境 Tracer，Registry 回退到 `current_tracer()` |
| `JsonRpcRequest`（未使用） | MCP `handle()` 用它校验请求，非法请求返回 -32600 |
| `PARSE_ERROR` / `INVALID_REQUEST` 常量 | 在 MCP Server 中实际使用 |
| `Trace.spans_of` | 评测判分改为调用它 |
| `Trace.children`、`utils.count_tokens/chunked/compact_whitespace/write_json/read_json` | 无调用者，删除 |
| `VectorStore.remove_document` / `clear` | 接入 `KnowledgeBase.delete_document()` 与全量重建索引，并暴露 `DELETE /kb/documents/{id}` |

### I. 文档描述但代码不存在的功能（已修正）

| 文档声称 | 初始真相 | 现在 |
| --- | --- | --- |
| 工具缓存 | 实现了但从未启用（TTL 默认 0），且缓存键被截断 | 默认 60s、键为完整参数 SHA-256、有测试 |
| LLM 重排 | 代码存在，但被 KnowledgeBase 自身 settings 固定，运行时不可触发 | 运行时 settings 生效，有测试 |
| 知识库重建索引 | 只做追加，磁盘删除的文档仍残留 | 全量重建，有回归测试 |
| 依赖"兼容版本" | fastapi 0.110.3 与 starlette 1.6 不兼容，应用无法构造 | 选定并验证兼容对 0.112.4 / 0.38.6 |
| 前端模型参数 | 缺 `max_tokens` | 已补齐并纳入契约测试 |

### J. 代码实现了但文档未说明（已补充）

`DELETE /kb/documents/{doc_id}`、`tool_cache_ttl_s`、`web_search_allowed_hosts`、指标分母语义、
「无证据 → degraded」状态语义、`/health` 暴露真实 MCP 传输、MCP 启动重试与回退标记。

---

## 八项「接线」专项检查

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| 未注册 Tool | ✅ 无 | `build_default_registry` 注册 6 个工具；`/health` 返回 6 个；`test_tools.py` 校验 schema/permission/timeout |
| 未连接 Agent | ✅ 无 | 一次真实运行产生 5 个 agent span：Planner/Researcher/Verifier/Critic/Writer |
| 未连接 API | ✅ 无 | `POST /research` → ServiceContainer → pipeline → agents → tools 全链；`test_research_endpoints_and_trace` 覆盖 5 个 research 端点 |
| 前端调用不存在的后端接口 | ✅ 无 | `test_frontend_calls_only_existing_api_routes`：app.js 中每个 URL 都能在 OpenAPI 路由表找到 |
| 后端存在但前端未使用 | ⚠️ 有意保留 | `/research/{id}/sources`、`/research/{id}/metrics`、`GET /kb/documents/{id}`、`POST /kb/documents`、`/mcp/call` 面向程序化调用；UI 使用聚合结果，已在 `docs/api.md` 说明 |
| MCP 只有类定义、无真实调用链 | ✅ 已闭环 | `test_api_container_uses_http_mcp_server_process`：真实启动 MCP HTTP 服务进程，API 通过 HTTP 调用并产生 mcp span；另有 stdio 子进程与 in-process 测试 |
| RAG 定义但未进入 Agent Workflow | ✅ 已闭环 | 真实运行 trace 含 `Retriever.retrieval[hybrid]` span（6 命中 / 6 doc_ids）；`knowledge_search` 工具即 RAG 入口 |
| Evaluation 定义但未被执行 | ✅ 已闭环 | `scripts/run_benchmark.py` 35 任务全跑；`tests/evaluation/test_smoke.py` 在 CI 中作为门槛；`docs/evaluation.md` 自动生成 |
| Trace 部分 Agent 缺失 | ✅ 无 | trace 中 agent 层级为 Planner/Researcher/Verifier/Critic/Writer 全 5 个；另有 LLM/Tool/Retrieval/Retry span；字段含 task/trace/agent/start/end/latency/model/tool/input/output/error/usage |

---

## 发现分类（BUG / PARTIAL / MISSING / DEAD CODE / FAKE FEATURE / TECH DEBT）

| 类别 | 条目 |
| --- | --- |
| **BUG**（功能存在但实现错误） | **存储不可用时 `pipeline.run()` 直接抛异常**（不可写 `runs_path` 导致 API 500；启动阶段索引写入失败也会崩）； 评测基线依赖**未声明**的可选分词依赖 `jieba`（同一份代码在干净环境 30/35、装有 jieba 35/35，A/B 证实）；长期记忆使用进程级默认路径而非注入的 `runs_path`（写失败还会让任务失败）；MCP HTTP 应用的 `/openapi.json` 因函数内导入 `JSONResponse` 抛 `PydanticUserError`；**切换 embedding 提供方后静默复用旧索引向量**（维度不匹配仍照查）；**弱模型返回「schema 合法但无引用」的空报告时仍照原样输出**；缓存键截断导致错误命中；Planner 在向量库不可用时崩溃；reindex 残留已删除文档；UTC 时间被当本地时间（延迟虚高 8h）；MCP 工具在无 client 时仍注册；`/mcp` 的 `Request` 注解被当成查询参数；fastapi/starlette 不兼容 |
| **PARTIAL**（只实现一部分） | 工具缓存未启用；LLM 重排不可触发；KB 删除能力缺失（新增）；前端缺 `max_tokens`/评测面板 |
| **MISSING**（需求明确但完全缺失） | 真实 HTTP provider 无任何端到端验证；无 `requirements.txt`；无依赖兼容性测试；无前端↔后端契约测试；无 SSRF 防护 |
| **DEAD CODE** | `_by_doc`、`tracer_scope/current_tracer`、`JsonRpcRequest`、`PARSE_ERROR/INVALID_REQUEST`、`Trace.children`、`utils` 中 5 个未用函数、`VectorStore.remove_document/clear`（后两项改为接入真实功能） |
| **FAKE FEATURE**（表面存在但实际不工作） | 文档与配置中的"工具缓存"（未启用）；"LLM 重排"（不可达）；"全量重建索引"（实为追加） |
| **TECH DEBT** | JSON 单机持久化；串行子任务；线程池超时不可中断；哈希向量语义弱；无多租户鉴权；SSE 流式输出缺失 |

---

## 严重程度分布

| 级别 | 数量 | 内容 |
| --- | --- | --- |
| P0 | 0 | 初始状态可运行 |
| P1 | 2 | Planner 崩溃于向量库故障；真实 provider 链路未验证 |
| P2 | 16 | **存储失败未降级**；缓存键/缓存未启用、指标真空 1.0、cwd 依赖路径、Prompt 非 JSON、Agent 内 if/elif、故障注入未生效、无证据仍成功、LLM 重排不可达、reindex 残留、依赖不兼容、长期记忆路径错误、评测指标依赖未声明的 jieba、**embedding 切换后索引未失效**、**弱模型空报告无引用回退缺失** |
| P3 | 11 | 记忆命中未落盘、死代码、`.env.example` 不同步、前端缺字段/面板、`latency_ms=0`、无 requirements、SSRF、注入检测漏检、MCP 回退不可见、MCP `/openapi.json` 崩溃、校验器可被自洽但未落地的句子骗过 |
| P4 | 3 | docker 引擎不可用（已穷尽 PATH/常见安装路径/podman/buildah/nerdctl/WSL 确认）、无 npm 构建链（已用零依赖 node 测试覆盖前端逻辑）、离线指标不代表模型质量（已如实标注） |
| **合计** | **32**（修复 30 + 环境限制 2） | |

---

## 验证证据（全部真实运行）

| 验证 | 命令 / 方式 | 结果 |
| --- | --- | --- |
| 静态检查 | `ruff check .` / `ruff format --check .` / `mypy researchpilot` | 全部通过（0 error） |
| 测试 | `python -m pytest -q` | **170 passed**（含 unit / integration / evaluation）+ 9 个前端 node 测试 |
| 依赖一致性 | `python -m pip check` | 本项目 fastapi/starlette 冲突消失；余下为环境内无关预装包 |
| 干净环境安装 | `python -m venv` + `pip install -e .`（CI 路径用 `.[dev]`） | 成功解析并安装 fastapi 0.112.4 / starlette 0.38.6 / jieba 0.42.1 等；CLI、uvicorn 与 CI 的 lint/mypy/pytest 步骤均在干净环境内跑通 |
| 指标可复现性 A/B | 同一 venv、同一代码，仅差 `jieba` | 无 jieba：30/35、Citation 87.9%；有 jieba：35/35、Citation 100% → 已将 `jieba` 声明为硬依赖 |
| 真实 HTTP provider | 本地 OpenAI 兼容端点（chat + embeddings） | 7 项测试全绿：结构化输出、鉴权、5xx 重试、401 映射、超时、embeddings、成本 |
| **真实模型端到端** | 本地 `Qwen1.5-0.5B-Chat`（GPU）+ `text2vec-base-chinese`(768d) | 探针 1 次通过（attempts=1, 520 tokens, 0 repairs）；流水线 3 任务 1/3、Recall 100%；新增空报告回退后同任务引用正确率 100%（`docs/evaluation_local_model.md`） |
| 检索消融（真实数据） | `scripts/retrieval_ablation.py` | hash：dense/keyword/hybrid Recall 80.6/82.2/82.2%、Precision 43.3/50.6/51.7%；text2vec-768d：72.2/82.2/82.2%、47.8/50.6/51.7%，hybrid MRR 0.796（`docs/retrieval_ablation.md`） |
| MCP 双服务链路 | 启动 MCP HTTP 服务进程 | API `/health` 显示 `mcp_transport=http`，`/mcp/tools` 走 HTTP，研究任务产生 mcp 调用 |
| 完整链路 | CLI 真实运行（"分析当前大模型 Agent 在软件开发中的主要应用方向…"） | Planner 3 子任务 → 3 次工具调用 → 9 条证据 → Verifier 9 checks（sufficient=True）→ Critic → Writer 3 节/5 结论 → 9 条引用全部可解析 → Trace{agent:5, llm:7, tool:3, retrieval:1} |
| 离线 Benchmark | `scripts/run_benchmark.py --provider mock` | 35/35 通过；Recall 93.9%（分母 30）、Context Relevance 28.8%、Citation 100%（分母 34）、Tool F1 85.9%、Tool Success 74.6%、平均延迟 0.31s |
| Docker 构建 | **未实机执行**（本环境无 docker 引擎，已穷尽 PATH/安装路径/podman/buildah/nerdctl/WSL） | 等价证据：① `tests/unit/test_docker_assets.py` 校验 compose 结构与依赖条件、环境变量名、`COPY` 源与 `.dockerignore`、CMD 可导入、healthcheck 路由；② 干净环境 `pip install -e .` 与镜像内 CMD 实机跑通；③ **`scripts/compose_smoke.py` 用真实 compose 文件起双服务**：MCP 健康 → API `mcp_transport=http` → `/mcp/tools` 走 HTTP → 前端 200 → 研究任务 `mcp_calls=1` |

---

## 仍然存在的问题

1. **厂商模型未调用，但真实模型已完成端到端验证**：环境内的 `OPENAI_API_KEY` 对 `api.openai.com` 返回
   **HTTP 401 `invalid_api_key`**，无法用于线上模型；因此改用**本地真实模型**
   （`Qwen/Qwen1.5-0.5B-Chat` + `shibing624/text2vec-base-chinese`，GPU，自实现 OpenAI 兼容端点）跑通整条链路：
   结构化输出探针 1 次通过（attempts=1, 520 tokens）；端到端 3 条任务 1/3 通过、Retrieval Recall 100%；
   定位到「弱模型返回空报告 → 0 引用」后新增空报告回退，复测同任务**引用正确率 100%**。
   详见 `docs/evaluation_local_model.md`。换成线上有效 Key 只需
   `export API_KEY=sk-... && python scripts/verify_live_model.py --limit 35`。
2. **Docker 引擎仍缺失，但 compose 拓扑已用真实 compose 文件验证**：本机无 `docker`/`podman`/`buildah`/
   `nerdctl`，WSL 无发行版，因此 `docker build` / `docker compose up` 无法在此环境执行。
   新增 `scripts/compose_smoke.py`（+ `tests/integration/test_compose_topology.py` + CI 步骤）：
   读取真实 `docker-compose.yml`，按 `depends_on: service_healthy` 先起 MCP 再起 API，实测
   `mcp_transport=http`、`/mcp/tools` 走 HTTP、前端 200、研究任务 `mcp_calls=1`，并在宿主路径映射处打印每条 remap。
   在有 Docker 的机器上执行 `docker compose up --build` 仍是唯一未做的动作。
3. **Context Relevance 28.8%**：哈希向量语义弱，需真实 embedding 与交叉编码器重排。
4. **串行子任务**、**线程池超时不可中断**、**JSON 单机存储**、**无多租户鉴权/流式输出**：见 Future Work。
5. **语义类质量控制**仍以代码规则为准，LLM judge 默认关闭以保证可复现。
6. **离线 provider 的 35/35** 只证明工程契约满足，不代表模型生成质量（已在 README 与自动生成文档中标注）。
