# API Reference

Base URL：`http://127.0.0.1:8000`（`python -m researchpilot.cli serve`）
交互式文档：`/docs`（Swagger UI）与 `/openapi.json`。

所有请求/响应均为 JSON，模型由 Pydantic 定义并做参数校验；校验失败返回 `422`。

---

## Meta

### `GET /health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "provider": "mock",
  "model": "mock-research-model",
  "knowledge_base": {"documents": 10, "chunks": 39, "characters": 9391, "dimension": 384},
  "tools": ["calculator", "document_reader", "knowledge_search", "mcp_research_context", "metadata", "web_search"]
}
```

### `GET /config`

返回 provider、可选模型、生成参数默认值与上限（前端据此渲染 Settings 面板）。

---

## Research

### `POST /research`

请求体：

```json
{
  "question": "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。",
  "mode": "sync",
  "max_sources": 8,
  "settings": {
    "model": "gpt-4o-mini",
    "temperature": 0.2,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "max_tokens": 1200,
    "top_k": 6,
    "max_iterations": 2,
    "token_budget": 80000
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `question` | 4–2000 字符，去除首尾空白 |
| `mode` | `sync` 直接返回完整结果；`async` 立即返回 `202` + `task_id` |
| `settings` | 每请求覆盖项；`null` 表示使用服务端默认 |

`mode=sync` 响应（<code>ResearchResult</code>）：

```json
{
  "task_id": "task_530c154c04b7",
  "trace_id": "trace_530c154c04b7",
  "status": "succeeded",
  "question": "...",
  "plan": {"objective": "...", "subtasks": [{"id": "S1", "intent": "knowledge_search", "tools": ["knowledge_search"]}]},
  "evidence": {
    "evidence": [{"id": "E1", "claim": "...", "quote": "...", "source_id": "kb-tool-calling-mcp#c002"}],
    "sources": [{"id": "kb-tool-calling-mcp#c002", "kind": "knowledge_base", "title": "工具调用与 MCP 协议", "score": 0.48}]
  },
  "verification": {"checks": [{"evidence_id": "E1", "status": "supported", "overlap": 1.0}], "sufficient": true, "coverage_score": 1.0},
  "critique": {"issues": [], "needs_more_research": false, "follow_up_queries": []},
  "report": {"title": "研究报告：...", "conclusions": [], "sections": [], "markdown": "# 研究报告 ..."},
  "metrics": {"latency_ms": 3800.2, "tool_calls": 11, "llm_calls": 14, "usage": {"total_tokens": 42278}},
  "errors": []
}
```

`status` 取值：`pending` / `running` / `succeeded` / `degraded` / `failed`。
`degraded` 表示结果可用但存在失败（例如某个工具超时），`errors[]` 会给出具体原因。

`mode=async` 响应（HTTP 202）：

```json
{"task_id": "task_...", "status": "pending", "message": "poll /research/{task_id}"}
```

### `GET /research?limit=20`

最近任务列表（`task_id` / `question` / `status` / 时间 / 引用数 / 延迟 / 错误数）。

### `GET /research/{task_id}`

完整 `ResearchResult`；未知 id 返回 `404`。

### `GET /research/{task_id}/trace`

```json
{
  "trace_id": "trace_...",
  "task_id": "task_...",
  "spans": [
    {"span_id": "span_...", "parent_id": null, "name": "PlannerAgent", "kind": "agent", "latency_ms": 12.4,
     "usage": {"total_tokens": 900, "cost_usd": 0.0002}},
    {"span_id": "span_...", "parent_id": "span_...", "name": "PlannerAgent.llm[planner]", "kind": "llm", "model": "gpt-4o-mini"}
  ],
  "metrics": {"latency_ms": 3800.2, "llm_calls": 14, "tool_calls": 11, "retrieval_calls": 4, "retries": 0,
              "agent_latency_ms": {"PlannerAgent": 210.5}, "usage": {"prompt_tokens": 30000, "completion_tokens": 12000, "total_tokens": 42000}}
}
```

### `GET /research/{task_id}/sources`

```json
{"task_id": "task_...", "sources": [{"id": "kb-tool-calling-mcp#c002", "kind": "knowledge_base", "title": "工具调用与 MCP 协议", "locator": "internal://handbook/tool-calling", "retrieved_at": "2026-09-13T07:00:00Z", "score": 0.48}]}
```

### `GET /research/{task_id}/metrics`

返回 `TaskMetrics`：`latency_ms`、`llm_calls`、`tool_calls`、`tool_failures`、`retrieval_calls`、
`mcp_calls`、`retries`、`iterations`、`usage{prompt_tokens, completion_tokens, total_tokens, cost_usd}`、
`agent_latency_ms`。

---

## Knowledge Base

### `GET /kb/documents`

`{"documents": [{"doc_id": "...", "title": "...", "chunks": 4, "characters": 812}], "stats": {...}}`

### `GET /kb/documents/{doc_id}`

文档元数据 + 分块列表（`chunk_id` / `section` / `characters` / `preview`）。

### `GET /kb/search`

参数：`q`（必填）、`top_k`（1–20）、`strategy`（`dense|keyword|hybrid`）、`rewrite`（bool）。

```json
{
  "query": "工具注册表需要声明哪些字段？",
  "rewritten_query": "工具注册表需要声明哪些字段？ —— ...",
  "strategy": "hybrid",
  "latency_ms": 6.2,
  "hits": [{"rank": 1, "chunk_id": "kb-tool-calling-mcp#c000", "title": "工具调用与 MCP 协议",
            "score": 0.51, "section": "工具调用与 MCP 协议 > 统一工具注册表", "components": {"body_coverage": 0.42}}]
}
```

### `POST /kb/documents`

```json
{"title": "内部规范", "content": "≥20 字符的正文…", "source": "api://inline", "metadata": {"topic": "internal"}}
```

响应：`{"documents": 1, "chunks": 3, "characters_in": 900, "characters_out": 1100, "duration_ms": 12.5}`

### `POST /kb/reindex`

全量重建索引：先清空现有 chunk，再重新读取 `RESEARCHPILOT_KB_PATH` 下的全部文档。
磁盘上被删除/改名的文件不会残留在索引中（返回入库统计）。

### `DELETE /kb/documents/{doc_id}`

删除文档及其全部 chunk：

```json
{"doc_id": "kb-fixture-memory", "chunks_removed": 1, "stats": {"documents": 4, "chunks": 9}}
```

未知 `doc_id` 返回 `404`。

---

## MCP

### `GET /mcp/tools`

```json
{"transport": "inprocess", "server": "researchpilot-mcp",
 "tools": [{"name": "search_knowledge", "description": "...", "inputSchema": {...}}]}
```

### MCP STDIO 传输的编码契约

`python -m researchpilot.mcp_server --stdio` 使用换行分隔的 JSON-RPC 2.0，编码固定为 UTF-8，
与操作系统 locale 无关（Windows 上默认可能是 `cp936`/`gbk`，会导致中文参数与结果损坏）：

- 服务端启动时把 `stdin`/`stdout` 显式重配为 UTF-8（`errors="strict"`，`newline="\n"`），并直接读写底层字节缓冲；
- 请求行若不是合法 UTF-8，服务端返回 `-32700` PARSE_ERROR 并继续服务（不静默、不崩溃）；
- 启动横幅与所有日志一律写 stderr，stdout 只承载协议消息；
- 客户端以 `-X utf8` + `PYTHONIOENCODING=utf-8` 启动子进程，并按 `encoding="utf-8", errors="strict"` 解码。

客户端示例（Python）：

```python
from researchpilot.mcp.client import StdioMcpClient

with StdioMcpClient() as client:  # 或 StdioMcpClient(command=[...])
    result = client.call_tool("search_knowledge", {"query": "工具注册表", "top_k": 2})
```

### `POST /mcp/call`

```json
{"name": "get_research_context", "arguments": {"query": "工具权限", "subtask": "S1", "top_k": 3}}
```

响应为 MCP `tools/call` 结果：`content`（文本）、`structuredContent`（结构化数据）、`isError`、`_meta.latency_ms`。

---

## Evaluation

### `GET /evaluation/latest`

返回最近一次 benchmark 的摘要（`generated_at` / `provider` / `model` / `metrics` / `categories` / `failed_checks`）；
若尚未运行过 benchmark 返回 `{"available": false}`。

---

## 错误语义

| 状态码 | 场景 |
| --- | --- |
| `422` | 请求体/参数不符合 Schema（FastAPI + Pydantic） |
| `404` | 未知 `task_id` / `doc_id` |
| `429` | token 预算耗尽（`BudgetExceededError`） |
| `502` | LLM 或 MCP 调用失败（上游错误） |
| `503` | Provider 配置缺失（如选择了 `openai` 但未设置 `API_KEY`） |

所有响应都带 `X-Process-Time-Ms`；服务端日志记录方法、路径、状态码与耗时。

### 运行时故障 vs 部署期故障

| 场景 | 行为 | 原因 |
| --- | --- | --- |
| 部署期 provider 配置错误（如 `provider=openai` 但没有 `API_KEY`） | 启动即失败（`LLMConfigError`，附带修复提示） | 配置错误应当 fail-fast，不能静默退回 mock 模型 |
| 运行时上游不可用（网络错误、超时、5xx、本地检索已成功的场景） | `200` + `status="degraded"`（或 `failed`），`errors[]` 记录原因，报告由本地证据生成或显式声明缺口 | 研究任务本身仍然产出一份可审计的结果文档 |
| 请求体/参数非法 | `422` | FastAPI + Pydantic 校验 |
| 未知 task/doc | `404` | 资源不存在 |
| token 预算耗尽 | `429` | `BudgetExceededError` |
| 运行目录不可写（只读/磁盘满/路径被文件占用） | 同步 `POST /research` 返回 `200` + `status=degraded`（`errors` 含 `persistence[...]`）；`mode=async` 提交返回 `503` + `kind=storage_unavailable` | 索引/结果无法落盘时，内存中的结果仍然可用；服务不会崩溃 |

### `GET /health` 与 MCP 传输

`/health` 返回实际生效的 MCP 传输（`inprocess` / `stdio` / `http` / `inprocess-fallback`）。
当 API 与 MCP 作为两个容器部署时（`docker-compose.yml`），API 会重试连接 MCP 服务；
若最终失败则回退到进程内客户端，并在 `/health` 中显式暴露该回退，避免"看起来走了 MCP 其实没有"。
