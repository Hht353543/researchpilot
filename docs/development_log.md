# Development Log

按用户要求的 16 个 Phase 记录：每个阶段做了什么、跑了什么验证、发现了什么问题以及如何修复。
本文件是开发过程的真实记录（包含失败与修复），不是事后美化的总结。

## Phase 1–3 · 仓库分析与架构设计

- 结论：工作区 `C:\Users\22394\Desktop\workflow` 为空目录（无 git、无既有代码），因此这是**全新项目**，
  "优先复用已有代码"在本次执行中不适用，未做任何推翻式重写。
- 技术栈选择（依据本机可用依赖）：Python 3.12 + Pydantic v2 + FastAPI + httpx；
  检索/BM25/MCP 协议等关键模块自行实现，避免引入无必要框架、并保证无网络也能运行。
- 产出：`docs/architecture.md`（组件边界、数据流、失败处理、取舍）、ADR-0001…ADR-0005。

## Phase 4 · 核心 Agent Workflow

- 实现 Planner / Researcher / Verifier / Critic / Writer 与 `ResearchPipeline` 迭代控制。
- 验证：`tests/integration/test_pipeline.py`（端到端报告、Trace 层级、降级路径、异步提交）。
- 发现并修复：
  1. 早期 Verifier 只依赖模型自评 → 改为在代码中重新推导全部判定（引用存在性/quote 归属性/相关性/来源质量）；
  2. 工具失败只记录到 metrics、不影响状态 → 修复为写入 `errors` 并让状态变为 `degraded`；
  3. 无证据时仍输出"看似合理"的报告 → 增加显式缺口声明与 limitations。

## Phase 5 · Tool Registry + Tool Calling

- 统一工具契约（schema/permission/timeout/retry/cache/Trace/调用预算），无 if/elif 分发。
- 验证：`tests/unit/test_tools.py`（参数校验、权限策略、超时重试、预算、缓存）、`tests/unit/test_calculator.py`（AST 白名单）。
- 发现并修复：MCP 工具在缺少 client 时被注册 → `ToolContext.require` 对 `None` 抛错 + 注册表按服务可用性裁剪工具。

## Phase 6 · RAG

- 实现 Loader/Cleaner/Chunker/Embedding/VectorStore/Retriever(idf rerank)/KnowledgeBase。
- 验证：`tests/unit/test_rag_pipeline.py`（10 项），API `/kb/*` 集成测试。
- 发现并修复（重要）：
  1. YAML front matter 的日期被解析成 `datetime`，JSON 落盘失败 → 增加 `_jsonify`；
  2. chunk 内容缺少标题路径，导致"三种主流控制流"等小节无法被检索 → chunk 前缀章节路径；
  3. 重叠窗口从句子中间开始，把 `JSON-RPC` 截断成 `RPC` → 重叠改为按句边界对齐；
  4. `token_set` 对 CJK 生成跨标点的无意义 bigram（如 `与关`），污染相关性计算 → 仅在连续 CJK 串内生成 bigram，
     并在 topical relevance 中使用 jieba 词级 token。

## Phase 7 · MCP

- 实现 MCP Server（initialize / tools/list / tools/call）与 in-process / stdio / streamable-HTTP 三种传输。
- 验证：`tests/integration/test_mcp.py`（含子进程 stdio 往返、ASGI HTTP 往返、批量请求、错误码）。
- 发现并修复：`from __future__ import annotations` + 函数内局部 import 使 FastAPI 无法解析 `Request` 注解，
  `/mcp` 被当成查询参数 → 改为 `body: Any = Body(...)`。

## Phase 8 · Memory

- 三层记忆：短期（token 预算滚动）、工作记忆（计划/子任务/证据/观测，hash+Jaccard 去重）、
  长期记忆（TTL/去重/重要度/来源）。
- 验证：`tests/unit/test_memory.py`。

## Phase 9 · Trace / Observability

- Tracer（span 树 + ContextVar 自动嵌套）、TraceStore（落盘）、成本估算（价格表来自 `configs/pricing.yaml`）。
- 验证：`tests/unit/test_observability.py`。
- 发现并修复：`time.mktime` 把 UTC 时间戳当本地时间，导致延迟虚高 8 小时 → 改用 `calendar.timegm`。

## Phase 10 · Evaluation

- 35 条 Golden Dataset（12 类场景，含故障注入/注入攻击/无结果/低质量来源）+ 代码判分 + 指标聚合 + 报告生成。
- 验证：`tests/evaluation/`（数据集完整性、runner 机制、全量离线冒烟）。
- 发现并修复（迭代最多的一环）：
  1. 引用正确率在没有引用时被算成 0 而非"未定义" → 明确定义并在数据集中要求引用强度；
  2. 相关性判定过严/过松：先用句级重叠、再改 chunk 级（标题+正文）覆盖度，最终使用 jieba 词级
     `content_overlap` 并以 plan objective 作为参考；
  3. 证据抽取被单一来源垄断 → 改为"每次工具调用一个证据桶 + 桶内按检索顺序分配段落配额"；
  4. 含提示注入的文本会被当成结论引用 → Writer 阶段排除含注入特征的证据，并在 limitations 中列出。

## Phase 11 · FastAPI

- 11 组端点（research / trace / sources / metrics / kb / mcp / evaluation），统一错误语义与请求耗时日志。
- 验证：`tests/integration/test_api.py`（8 项）+ 真实 `uvicorn` 启服手工验证（health/config/index/research/trace/mcp）。
- 发现并修复：
  1. `ResultStore` 把 `runs/knowledge_base.json` 当作结果文件解析 → 结果文件改用 `*.result.json` 后缀并容错跳过；
  2. 本机 `fastapi 0.110.3` 与 `starlette 1.6.0` 不兼容（`Router(on_startup=...)` 已被移除）→ 安装匹配的
     `starlette 0.39.2`；README 记录已测组合，CI 由 `pip install -e .` 自动解析兼容版本。

## Phase 12 · Frontend

- 零依赖单页前端：Research Input / Model Settings / Knowledge Base / Agent Timeline / Final Report /
  Metrics / Sources / MCP 工具，Markdown 渲染器自带（离线可用）。
- 验证：`GET /` 与 `/static/app.js` 集成测试 + 真实服务访问。

## Phase 13 · Tests / Docker / CI

- 测试：88 个（unit / integration / evaluation）+ fixtures。
- Dockerfile（含 healthcheck）、docker-compose（api + mcp 两个独立服务）、GitHub Actions（lint / typecheck /
  test+cov / evaluation smoke / docker build）。
- 验证：`ruff check`、`ruff format --check`、`mypy researchpilot`、`pytest -q` 全部通过。

## Phase 14 · 运行完整测试

```
ruff check .                → All checks passed!
ruff format --check .       → 102 files already formatted
mypy researchpilot          → Success: no issues found in 69 source files
python -m pytest -q         → 88 passed
```

## Phase 15 · 运行 Benchmark

```
python scripts/run_benchmark.py --provider mock
tasks=35 passed=35 success=100.0%  recall=94.8%  context_relevance=38.9%  citation=97.1%
tool_selection_accuracy=100%  tool_f1=85.9%  tool_success=75.2%
avg_latency=0.33s p95=1.37s tokens=658,286 cost=$0.000000
```

数字来源：`benchmarks/latest_mock.json`；渲染结果：`docs/evaluation.md`。

## Phase 16 · 文档与简历描述

- `README.md`（含诚实声明）、`docs/architecture.md`、ADR、`docs/api.md`、本文件。
- `docs/evaluation.md`、`docs/resume.md` 由 `scripts/run_benchmark.py` 依据实测数据自动生成。
- `examples/sample_report.md`、`sample_result.json`、`sample_trace.json` 由 `scripts/run_demo.py` 生成。

---

## 仍存在的问题（Unknowns / Known Limitations）

1. **离线指标不等于模型质量**：`provider=mock` 是确定性脚本模型，35/35 说明工程契约满足，
   不代表真实模型在开放问题上的表现。真实模型评测需 `--provider openai` 重新运行。
2. **Context Relevance 偏低（39.4%）**：默认哈希向量语义泛化有限；真实 embedding + 交叉编码器重排是改进方向。
3. **子任务串行执行**：端到端延迟随子任务数线性增长（本项目为可测试性选择同步实现）。
4. **工具超时无法真正中断线程**：`ThreadPoolExecutor` 无法强杀阻塞线程，超时后后台线程仍会跑完
   （已记录在案；生产环境建议使用可取消的异步客户端或子进程隔离）。
5. **判分为代码规则**：语义类问题（论证质量、表达清晰度）未被量化，需要人工抽样或引入可复现的
   LLM 判分（默认关闭以保证可复现）。
6. **JSON 持久化**：适合单机与演示，不适合大规模并发；需替换为数据库/对象存储。
7. **Web 检索默认离线语料**：`data/web_corpus` 为合成数据，真实检索需配置 `WEB_SEARCH_MODE=http` + 真实 API。
8. **本工作区的 `.git` 目录为只读挂载**：无法在此环境中执行 `git init/commit`（写 `.git/index.lock` 被拒绝），
   因此没有自动创建初始提交。请在本地执行：

   ```bash
   cd C:\Users\22394\Desktop\workflow
   git init -b main && git add -A && git commit -m "feat: ResearchPilot initial implementation"
   ```
