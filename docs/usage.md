# 使用说明

这份文档面向第一次拿到仓库的人：从零装好、跑通一次研究、看懂产物，再按需接入真实模型或容器。
所有命令都在仓库根目录执行。

## 1. 先跑起来（离线，不需要 API Key）

```bash
python -m venv .venv              # Python 3.11+；不要继承全局 site-packages
# 激活 .venv 后：
python -m pip install -c constraints.txt -e ".[dev]"
python -m pip check
python -m researchpilot.cli ingest                    # 把 data/knowledge_base 建索引
python -m researchpilot.cli research "分析当前 AI Agent 在企业软件开发中的应用趋势"
```

第三条命令会打印报告，同时在 `runs/` 下写任务元数据和运行产物：

| 文件 | 内容 |
| --- | --- |
| `runs/{task_id}.task.json` | 生命周期事实源：时间、owner/心跳、终态及 result/trace 引用状态 |
| `runs/{task_id}.result.json` | 完整结果：计划、证据、校验、批评、报告、指标 |
| `runs/{task_id}.trace.json` | 执行轨迹：Task → Agent → LLM / Tool / Retrieval / Retry |
| `runs/long_term_memory.json` | 跨任务记忆（偏好、主题、结论） |
| `runs/.long_term_memory.json.lock.sqlite3` | 长期记忆跨进程写锁；运行期间由程序维护 |
| `runs/.knowledge_base.json.lock.sqlite3` | 知识库索引跨进程写锁；运行期间由程序维护 |
| `runs/.recovery-observations.json` | 启动时发现的残留临时文件或损坏任务记录；仅登记，不自动删除 |

离线模式的意义：没有网络、没有 Key 也能把整条链路跑通，并且结果可重复。它衡量的是工程管线，
不是模型写得好不好。

## 2. 看前端和 API

```bash
python -m researchpilot.cli serve
# 浏览器打开 http://127.0.0.1:8000/
```

页面上有四块：研究输入与模型参数、知识库、Agent 时间线、报告与指标。后端是 FastAPI，常用端点：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/research` | 跑一次研究；`mode=async` 时返回 202 和 `task_id` |
| GET | `/research/{task_id}` | 取完整结果 |
| GET | `/research/{task_id}/trace` | 取执行轨迹 |
| GET | `/research/{task_id}/sources` | 取引用来源 |
| GET | `/research/{task_id}/metrics` | 取延迟、token、工具调用等 |
| GET | `/health` | 服务状态，含当前 MCP 传输方式 |

异步跑一次并轮询：

```bash
curl -s -X POST localhost:8000/research -H 'content-type: application/json' \
  -d '{"question":"MCP 和传统工具调用有什么区别？","mode":"async"}'
curl -s localhost:8000/research/<task_id> | head -c 400
```

完整接口清单见 [api.md](api.md)。

### 本地与共享运行边界

默认启动只监听 `127.0.0.1:8000`，适合本机开发。共享部署需要显式配置公开监听与访问 token：

```bash
export RESEARCHPILOT_BIND_HOST=0.0.0.0
export RESEARCHPILOT_ALLOW_REMOTE_ACCESS=true
export RESEARCHPILOT_ACCESS_TOKEN='replace-with-a-long-random-token'
python -m researchpilot.cli serve
```

API 客户端随后通过 `Authorization: Bearer ...` 发送 token。浏览器前端可在 Model Settings 中输入同一 token。
公网部署还应由反向代理提供 HTTPS；项目内置 token 只承担最小访问控制。

## 3. 接入真实模型

任何 OpenAI 兼容端点都能用（OpenAI、DeepSeek、vLLM、Ollama 等），只要给三个变量：

```bash
export RESEARCHPILOT_PROVIDER=openai
export RESEARCHPILOT_API_KEY=sk-...        # 也可以用 API_KEY
export RESEARCHPILOT_BASE_URL=https://api.deepseek.com/v1
export RESEARCHPILOT_MODEL=deepseek-chat
```

先做一次最小真实调用，确认凭据和端点没问题（脚本不会打印密钥）：

```bash
python scripts/verify_live_model.py --probe-only
```

通过后再跑真实模型评测：

```bash
python scripts/run_benchmark.py --provider openai
```

它会覆盖写入 `docs/evaluation.md`、`docs/resume.md`，并把逐任务原始数据留在 `benchmarks/` 下。
真实模型比 mock 慢、贵得多（2026-09-15 的 `deepseek-chat` 全量 35 条：约 65–100 分钟、
$1.16、单任务约 5.9 万 token）。想省钱可以先小范围试：

```bash
python scripts/run_benchmark.py --provider openai --limit 3
python scripts/run_benchmark.py --provider openai --categories rag citation
python scripts/run_benchmark.py --provider openai --task-ids fact-01-mcp-protocol
```

## 4. 用 MCP

MCP Server 可以独立运行，Agent 通过它拿知识库和检索能力。三种传输：

```bash
# 给 Claude Desktop / Cursor 这类客户端接入
python -m researchpilot.mcp_server --stdio

# 独立进程 + HTTP，可单独扩缩容
RESEARCHPILOT_MCP_TRANSPORT=http RESEARCHPILOT_MCP_HOST=127.0.0.1 python -m researchpilot.mcp_server
curl -s localhost:8765/health

# 单进程模式（默认，不需要额外进程）
python -m researchpilot.cli mcp --transport stdio
```

它对外暴露四个工具：`search_knowledge`、`get_document`、`search_web`、`get_research_context`。

## 5. 管理知识库

```bash
python -m researchpilot.cli ingest                      # 重建索引（磁盘上删掉的文件不会残留）
python -m researchpilot.cli ingest --path data/knowledge_base/05-evaluation-and-observability.md
python -m researchpilot.cli ingest --text "自定义知识片段" --title "临时笔记"
```

只依赖 HTTP 时用 `/kb/documents`（增）、`/kb/documents/{doc_id}`（删，级联删 chunk）、`/kb/reindex`（重建）。

## 6. 用 Docker 起整套服务

```bash
docker compose up --build
# API 与前端 http://localhost:8000/ ，MCP http://localhost:8765/health
```

Compose 起两个服务：`api`（FastAPI + 前端）和 `mcp`（MCP Server），API 等 MCP 健康后再启动，
并通过 HTTP 调用它。容器里的健康检查、主机侧验证和容器内 smoke test 由 CI 的 `docker` job 真实执行，
见 [.github/workflows/ci.yml](../.github/workflows/ci.yml)。

## 7. 跑测试与评测

```bash
python -m pytest -q                              # 全部测试
python -m pytest -q -m "not evaluation"          # 快速回归
ruff check . && ruff format --check . && mypy .
python scripts/run_benchmark.py --provider mock  # 离线评测基线，写 docs/evaluation.md
python scripts/compose_smoke.py                  # 没有容器引擎时验证 compose 拓扑
python scripts/verify_fresh_clone.py             # 从干净 clone 复现 CI，确认提交的仓库自洽
```

## 8. 常见问题

**报告里的引用是 `[E1]` 这种编号，去哪找对应来源？**
报告末尾的"参考文献"由代码从来源元数据生成；`/research/{task_id}/sources` 也能拿到完整来源列表。
编号只允许指向通过校验的证据，模型编造的编号会被剔除并记进 `dropped_citations`。

**`status` 与 `quality` 有什么区别？**
`status` 表示任务生命周期：完成、失败、取消或任务级超时。只有 `status=completed` 才有正式结果；此时
`quality=succeeded|degraded` 描述结果质量。没有证据、工具超时、模型输出被修复过等情况会得到
`completed/degraded`，执行中断则是 `failed`。

**为什么 mock 分数比真实模型高很多？**
mock 是确定性脚本模型，它衡量的是检索、工具、校验、引用绑定、追踪这些工程环节是否正常；
真实模型分数反映生成质量，两者不能直接比较。报表里始终标注 provider。

**`benchmarks/` 里的 JSON 是什么？**
每次评测的逐任务原始记录，`docs/evaluation.md` 与 `docs/resume.md` 里的每个数字都能回溯到它。

**没有 Docker 引擎怎么办？**
`python scripts/compose_smoke.py` 会按真实的 `docker-compose.yml` 在本机起两个进程，验证服务编排、
端口映射和健康检查；CI 上的 `docker` job 负责真机容器验证。

**能换成别的向量库吗？**
可以。`researchpilot/rag/` 里的 Retriever 只依赖 VectorStore 的接口（dense / keyword / hybrid 三种查询），
换成 pgvector 或 Qdrant 时保持这层接口即可，其余流程不用动。
