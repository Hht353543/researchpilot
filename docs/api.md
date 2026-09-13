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

重新读取 `RESEARCHPILOT_KB_PATH` 下的全部文档并重建索引（返回入库统计）。

---

## MCP

### `GET /mcp/tools`

```json
{"transport": "inprocess", "server": "researchpilot-mcp",
 "tools": [{"name": "search_knowledge", "description": "...", "inputSchema": {...}}]}
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
