# 需求追溯矩阵（Requirements Traceability）

本文件把用户提供的《全量代码审查 + Bug 修复》规格书**逐条**映射到当前仓库中的证据，
用于回答：*规格里的每一条要求，能否用可复现的证据证明已完成？*

状态含义：✅ 已用真实证据验证 ｜ ⚠️ 已实现但验证受限（附等价证据，已说明原因）

复现入口：

```bash
python -m pytest -q                     # 170 个测试（unit / integration / evaluation）
node --test "tests/frontend/**/*.test.mjs"   # 9 个前端逻辑测试
ruff check . && ruff format --check . && mypy researchpilot
python scripts/run_benchmark.py --provider mock   # 离线回归基线 35/35
python scripts/compose_smoke.py                   # 无引擎 compose 拓扑验证
python scripts/verify_live_model.py --probe-only  # 真实模型链路（本地模型或有效凭据）
```

---

## 一、核心任务（五层审查 + 修复闭环）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 代码 Bug 审查 | `docs/audit_report.md` BUG 分类 + 逐条修复；31 项发现中 29 项已修复 | ✅ |
| 架构 Bug 审查 | ADR-0001..0005、`docs/architecture.md`；修复 Planner 崩溃于向量库故障等架构级缺陷 | ✅ |
| 功能缺失检查 | 新增 KB 删除、全量重建、工具缓存启用、LLM 重排可达、前端 max_tokens/评测面板 | ✅ |
| 工程质量 | ruff / format / mypy / pip check / 170 pytest + 9 node / CI / Docker 资产测试 | ✅ |
| AI Agent 特有可靠性 | 注入隔离、工具预算、超时重试、token 预算、无证据降级、引用绑定、修复重试 | ✅ |
| 发现→定位→修改→补测试→运行→验证→更新文档 闭环 | `docs/development_log.md` 逐条记录（含失败与回退）；每项均有对应测试 | ✅ |

## 二、第一阶段（先扫描不改代码）A–J 输出

| 输出项 | 证据 | 状态 |
| --- | --- | --- |
| A 模块清单 / B 模块作用 / C 依赖关系 | `docs/audit_report.md` §A–C（17 模块 + 单向依赖图） | ✅ |
| D 已实现功能 | §D + README Project Overview | ✅ |
| E 部分实现功能 | §E（检索语义质量、并行、超时中断、语义判分、持久化） | ✅ |
| F Stub / TODO / FIXME | `rg` 扫描：TODO/FIXME/NotImplementedError/stub = 0；`pass` 8 处均非空实现 | ✅ |
| G 空实现 | 无（ABC 均有具体子类） | ✅ |
| H 可疑实现 | §G + BUG 分类逐条处置 | ✅ |
| I 未被调用代码 | §H：7 项死代码（接入真实调用链或删除，附清单） | ✅ |
| J 文档说已实现但代码没有 | §I：5 项（缓存/LLM 重排/reindex/依赖/前端参数）全部修正 | ✅ |

## 三、第二阶段（缺失搜索 + 8 项接线专项）

| 检查项 | 证据 | 状态 |
| --- | --- | --- |
| TODO/pass/NotImplementedError/stub/placeholder/return None 全量搜索 | §F；命中的 placeholder 均为有意语义（异步 pending、输入框文案） | ✅ |
| 未注册 Tool | `/health` 返回 6 个工具；`test_registry_lists_specs_with_schemas` | ✅ |
| 未连接 Agent | 真实运行 trace 含 5 个 agent span | ✅ |
| 未连接 API | `test_api.py` 覆盖 research/kb/mcp/evaluation 全端点 | ✅ |
| 前端调用不存在的后端接口 | `test_frontend_calls_only_existing_api_routes` | ✅ |
| 后端存在但前端未使用 | 5 个端点面向程序化调用，已在 `docs/api.md` 说明 | ⚠️ 有意保留 |
| MCP 只有类定义、无真实调用链 | in-process/stdio/HTTP 三传输测试 + 双服务 `mcp_calls=1` | ✅ |
| RAG 定义但未进入 Agent Workflow | 真实 trace 含 `Retriever.retrieval[hybrid]`（doc_ids/hits） | ✅ |
| Evaluation 定义但未执行 | `scripts/run_benchmark.py` 35 任务；CI 冒烟；`docs/evaluation.md` 自动生成 | ✅ |
| Trace 部分 Agent 缺失 | Planner/Researcher/Verifier/Critic/Writer 全 5 个 + LLM/Tool/Retrieval/Retry | ✅ |

## 四、第三阶段（Agent Workflow 真实调用链）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| Planner 生成结构化 Plan | `ResearchPlan` + `tests/unit/test_planner.py` | ✅ |
| Plan 驱动后续执行 | `_normalise` 校验工具可用性；Researcher 按 subtask.tools 执行（测试断言） | ✅ |
| Researcher 真的执行 Tool Calling | 真实运行 3 次工具调用；tool span 记录 arguments/attempts | ✅ |
| RAG 真的被使用 | `knowledge_search` → Retriever（retrieval span） | ✅ |
| MCP 真的被调用 | `mcp_research_context`；compose 拓扑实测 `mcp_calls=1` | ✅ |
| Evidence 保存 | `EvidenceBundle` + 工作记忆去重 + `runs/*.result.json` | ✅ |
| Verifier 真正验证 Evidence | 代码复核四项判定，覆盖 LLM 自评（单测） | ✅ |
| Critic 能触发重新研究 | `needs_more_research` + follow_up_queries → 迭代补检 | ✅ |
| Writer 只基于 Evidence 写作 | 引用绑定校验 + 注入证据隔离 + 空报告回退 | ✅ |
| Final Report 含 Citation | 真实运行 9 条引用全部可解析；参考文献由代码生成 | ✅ |

## 五、第四阶段（Agent 状态管理）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| State Schema / TypedDict / Pydantic | `researchpilot/schemas.py` | ✅ |
| agent state mutation / plan / evidence / task / retry state | `ResearchRuntime` + `WorkingMemory` + `TaskMetrics` | ✅ |
| 跨 Agent 字段一致性 | `test_cross_agent_state_contract_after_run` | ✅ |
| 无隐式全局状态 / 无跨任务泄漏 | 并发任务隔离测试 + `current_tracer()` 上下文测试 | ✅ |
| 无字符串拼接保存结构化状态 | `model_dump_json()` 往返一致测试 | ✅ |
| None 状态处理 | Schema 默认值 + 空知识库/无证据降级测试 | ✅ |

## 六、第五阶段（Tool Calling）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 统一 Registry（非核心 if/elif） | `tools/registry.py`；Agent 内已无工具名分支 | ✅ |
| name/description/input/output schema/permission/timeout/retry | `ToolSpec` + 相关单测 | ✅ |
| Schema 有效性 / 输出稳定 | Pydantic args_model；`/mcp/tools` 暴露 inputSchema | ✅ |
| 异常捕获 / timeout / retry 生效 | `test_registry_times_out_and_retries`、`test_registry_retries_until_success` | ✅ |
| permission 真正执行 | `test_default_policy_blocks_write_tools` 等 | ✅ |
| 调用产生 Trace | 每次调用生成 tool/mcp span（真实 trace 验证） | ✅ |
| 调用预算 | `ToolPolicy.max_calls_per_task` + 预算测试 | ✅ |
| 缓存 | 默认 60s TTL、完整参数哈希键、命中/未命中测试 | ✅ |

## 七、第六阶段（RAG）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| Loader→Cleaner→Chunker→Embedding→VectorStore→Retriever→Reranker→Context | `researchpilot/rag/*`（8 模块） | ✅ |
| 文档导入 / chunk / metadata / embedding | 10 篇 → 39 chunk；`test_rag_pipeline.py` 15 项 | ✅ |
| semantic / keyword / hybrid | 三策略测试 + `docs/retrieval_ablation.md` 实测 | ✅ |
| reranking 真的执行 | heuristic + LLM 两条路径均有测试 | ✅ |
| query rewrite 真的执行 | retrieval span 记录 `rewritten_query` | ✅ |
| retrieval 结果进入 Agent Context | `knowledge_search` 返回并写入证据 | ✅ |
| 空知识库 / 文档不存在 / embedding 失败 / 向量库不可用 / 无结果 的 fallback | 5 个专项测试（`test_pipeline.py`） | ✅ |

## 八、第七阶段（MCP）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 独立 MCP Server 可启动 | `python -m researchpilot.mcp_server`（stdio 与 HTTP 均实测） | ✅ |
| 暴露 search_knowledge / get_document / search_web / get_research_context | `/mcp/tools` 返回 4 个工具（inputSchema 完整） | ✅ |
| 参数验证 / 错误处理 / 超时 | `test_unknown_method_and_tool_errors`、`test_tool_call_returns_structured_content`、客户端超时配置 | ✅ |
| Agent ↔ MCP 完整闭环 | `test_api_container_uses_http_mcp_server_process` + `scripts/compose_smoke.py` | ✅ |
| MCP 是否只是"假的" | 否：真实 JSON-RPC 往返 + 双服务 HTTP（mcp span 与 `mcp_calls` 为证） | ✅ |

## 九、第八阶段（Memory）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 短期 / 工作 / 长期三层存在 | `researchpilot/memory/*` | ✅ |
| 写入 / 读取 / 更新 / 删除 | 三层各有测试；长期记忆 TTL 过期删除、去重更新 | ✅ |
| 去重 / importance / timestamp / TTL / source | `MemoryRecord` 字段 + 相关测试 | ✅ |
| Agent 真的使用 Memory | `test_memory_layers_are_actually_used`（运行后落盘主题与结论） | ✅ |
| 命中次数持久化 | `recall()` 后 flush + 单测 | ✅ |

## 十、第九阶段（Evaluation）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| tests/evaluation + Golden Dataset + benchmark + metrics 可运行 | `tests/evaluation/*`、35 条 / 12 类数据集、`scripts/run_benchmark.py` | ✅ |
| Task Success Rate | `docs/evaluation.md`（35/35 离线基线） | ✅ |
| Retrieval Recall / Context Relevance | 同上（分母 = 声明期望来源的 30 条） | ✅ |
| Citation Correctness | 同上（分母 = 要求引用的 34 条） | ✅ |
| Tool Selection Accuracy / Tool Success Rate | 同上（分母 35；含故障注入） | ✅ |
| Latency / Token / Cost | 来自 Trace 的真实测量；成本按 `configs/pricing.yaml` | ✅ |
| 禁止伪指标 | 移除真空 1.0（applicability 分母）、0 分母渲染 n/a、代码中无写死指标 | ✅ |
| Golden Dataset 非空非占位 | 35 条含期望来源/工具/判分条件；`test_dataset.py` 校验 | ✅ |

## 十一、第十阶段（Observability）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 每任务 task_id | `Trace.task_id` / `runs/*.trace.json` | ✅ |
| 每 Agent 记录 agent/start/end/latency | 5 个 agent span（真实 trace 输出） | ✅ |
| LLM 记录 model/token/latency | llm span + `usage`（真实模型 520 tokens 等） | ✅ |
| Tool 记录 tool/arguments/result/success/latency | tool span（含 attempts/error） | ✅ |
| Retrieval 记录 query/文档/分数/latency | retrieval span（rewritten_query/doc_ids/hits） | ✅ |
| 不存在部分链路无 Trace | `test_pipeline_records_trace_with_agent_hierarchy` + 真实 trace | ✅ |

## 十二、第十一阶段（LLM Provider）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| OpenAI 兼容抽象 | `llm/openai_provider.py`（httpx，base_url 可配，适用 OpenAI/DeepSeek/vLLM/Ollama） | ✅ |
| MODEL/API_KEY/BASE_URL/TEMPERATURE/PRESENCE/FREQUENCY/MAX_TOKENS 可配 | `config.py` AliasChoices + `.env.example` + `/config` | ✅ |
| 环境变量统一 / 默认值合理 / secrets 不泄露 | `test_api_key_never_leaks_into_responses_or_traces` | ✅ |
| .env.example 完整 | `test_env_example_documents_every_settings_field` | ✅ |
| 前端/后端参数一致 | `test_config_endpoint_matches_settings` + 前端契约测试 | ✅ |
| provider 不硬编码 | 工厂 + 配置（mock / openai 可切换） | ✅ |
| timeout / retry | provider 超时与 5xx 重试测试（真实 HTTP） | ✅ |
| pip check 与版本冲突 | 选定并验证 `fastapi 0.112.4 + starlette 0.38.6`；本项目依赖无冲突；其余为环境内无关预装包既有冲突 | ⚠️ 环境噪声已定位 |
| 基于实际 API 使用选择兼容版本 | `test_declared_ranges_accept_installed_versions`、`test_fastapi_and_starlette_are_a_compatible_pair` | ✅ |

## 十三、第十二阶段（API 全链路）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| POST /research 与 GET /research/{id}、/trace、/sources、/metrics | `tests/integration/test_api.py`（含 async 202 轮询） | ✅ |
| Frontend→HTTP→Service→Agent→Tools/RAG/MCP→Result 闭环 | 前端契约测试 + 真实 HTTP 端到端（本地真实模型、compose 拓扑） | ✅ |
| 404 / 422 / 500 / timeout / empty / invalid | 404、422、超时、空问题均有测试 | ✅ |
| LLM failure | 运行时故障 → 200 + `status=degraded/failed` + `errors` | ✅ |
| tool failure | 故障注入 4 类（超时/失败/空结果/预算） | ✅ |
| database / storage failure | 不可写 `runs_path`：同步研究降级返回、async 提交 503、启动不崩（`test_storage_failure_degrades_instead_of_crashing`） | ✅ |

## 十四、第十三阶段（Frontend）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| Research Input / Model Settings（model、temperature、presence、frequency、max_tokens） | `index.html` + `app.js`；`test_frontend_exposes_required_settings_fields` | ✅ |
| Agent Timeline / Knowledge Base / Final Report / Metrics | 7 个面板；`tests/frontend/app.test.mjs`（9 项） | ✅ |
| API URL / CORS / 类型 / 参数 / 响应结构 | CORS 中间件 + OpenAPI 契约测试 + 渲染测试 | ✅ |
| loading / error / empty state | `setStatus`、`renderTimeline(null)`、`renderSources([])`、`renderMetrics(undefined)` | ✅ |
| 不调用已变更/不存在的接口 | `test_frontend_calls_only_existing_api_routes` | ✅ |

## 十五、第十四阶段（测试体系，规格要求的 14 类）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 1 Planner | `tests/unit/test_planner.py`（6 项） | ✅ |
| 2 Tool Registry | `tests/unit/test_tools.py`（8 项） | ✅ |
| 3 Tool Permission | 同上（deny-list / 权限等级） | ✅ |
| 4 RAG Retrieval | `tests/unit/test_rag_pipeline.py`（15 项） | ✅ |
| 5 MCP | `tests/integration/test_mcp.py`（10 项，含 stdio 子进程与 HTTP 双服务） | ✅ |
| 6 Memory | `tests/unit/test_memory.py` + `tests/integration/test_state_and_memory.py` | ✅ |
| 7 Agent Workflow | `tests/integration/test_pipeline.py`（16 项） | ✅ |
| 8 Citation | `test_schemas_and_security.py` + citation 类别评测 | ✅ |
| 9 Retry | registry 重试 + provider 5xx 重试 | ✅ |
| 10 Timeout | registry 超时 + provider 超时 + timeout_recovery 评测 | ✅ |
| 11 Prompt Injection | `test_security_hardening.py` + 流水线注入隔离 + 评测类别 | ✅ |
| 12 Empty Retrieval | `test_empty_retrieval_is_reported_not_invented`、`test_no_sources_available_reports_explicit_gap` | ✅ |
| 13 LLM Failure | BrokenProvider 测试 + API 运行时故障测试 | ✅ |
| 14 API | `tests/integration/test_api.py`（14 项） | ✅ |
| 若原测试因修改失败则一并修复 | 全程无删测试/改测试造假；`docs/development_log.md` 记录每次回归与修复 | ✅ |

## 十六、第十五阶段（静态检查）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| ruff | `ruff check .` → All checks passed；`ruff format --check .` → 136 files formatted | ✅ |
| mypy | `mypy researchpilot` → Success（69 source files） | ✅ |
| pytest | 170 passed（+ 9 node 前端测试） | ✅ |
| pip check | 本项目依赖对无冲突；其余为环境内无关预装包冲突（逐条说明） | ⚠️ 环境噪声已定位 |
| 前端 npm test/build/lint | 无 npm 工具链（vanilla JS、无 package.json）→ `node --test`（零依赖 9 项）+ OpenAPI 契约测试等价覆盖 | ⚠️ 等价方案 |
| 不为绿色而关闭规则 | 仅对中文全角标点关闭 RUF001-003，并在 pyproject 中注明原因 | ✅ |

## 十七、第十六阶段（Docker）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| docker build | 本机无容器引擎（PATH / 常见安装路径 / podman / buildah / nerdctl / WSL 全部核实）→ 未执行 | ⚠️ |
| docker compose up（backend/frontend/vector store/MCP） | 未执行；等价验证：`scripts/compose_smoke.py` 用真实 compose 文件起双服务，实测 `mcp_transport=http`、前端 200、研究任务 `mcp_calls=1` | ⚠️ |
| healthcheck / 环境变量 / network / ports / volume | `tests/unit/test_docker_assets.py`（healthcheck 路由存在、env 名合法、端口/卷声明、`depends_on: service_healthy`） | ✅ |
| README 与实际 Docker 配置一致 | README Docker 章节与 `docker-compose.yml` 逐项对齐，并如实说明本机未跑引擎 | ✅ |

## 十八、第十七阶段（文档一致性）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| README / architecture / evaluation / ADR 与代码一致 | 全部按最终代码与实测数字更新；`docs/evaluation.md`、`docs/resume.md` 由脚本生成 | ✅ |
| 找出「文档说已实现但代码没有」 | `docs/audit_report.md` §J：5 项（缓存 / LLM 重排 / reindex / 依赖 / 前端参数） | ✅ |
| 找出「代码已实现但文档没说」 | §J 补齐（KB 删除、tool cache、allow-list、指标分母、degraded 语义、MCP 传输可见性） | ✅ |

## 十九、第十八阶段（安全审查）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| Prompt Injection | `security.py` 中英模式库 + 证据隔离 + 评测类别 + 单测 | ✅ |
| Tool Abuse | 权限分级 + 调用预算 + 参数校验 + 无破坏性工具 | ✅ |
| SSRF / 任意 URL | `HttpWebBackend` 仅 http(s)、拒绝 URL 内嵌凭据、可选 host allow-list（单测） | ✅ |
| 命令执行 | calculator AST 白名单（拒绝 `__import__`/`open`/lambda） | ✅ |
| 路径穿越 | document_reader / metadata 只接受索引 id（`../../etc/passwd` 测试） | ✅ |
| 敏感环境变量 / API Key 泄露 | Key 不出现在响应/trace/报告/落盘产物（专项测试）；`.env` 已在 .gitignore | ✅ |
| 恶意 Tool 参数 | Schema 校验 + 注入信号记录；非法参数返回结构化失败 | ✅ |
| 无限 Agent Loop | `max_iterations`、工具预算、token 预算、请求超时、重试上限均生效并被测试 | ✅ |

## 二十、第十九阶段（真实运行）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 实际跑完整流程（Planner→…→Writer） | CLI 真实运行：3 子任务 → 3 工具调用 → 9 证据 → 9 校验 → Critic → 3 章节/5 结论 → 9 引用全部可解析 | ✅ |
| 检查答案 / Citation / Trace / Metrics / Token / Latency | 上表 + `examples/sample_report.md`、`sample_trace.json`、`sample_result.json` | ✅ |
| 真实 LLM 调用 | 本地真实模型 `Qwen1.5-0.5B-Chat` + `text2vec-base-chinese`（GPU）：探针 attempts=1/520 tokens；流水线 Recall 100%；见 `docs/evaluation_local_model.md` | ✅ |

## 二十一、第二十一~二十四阶段（修复规则、分类与最终报告）

| 要求 | 证据 | 状态 |
| --- | --- | --- |
| 优先 P0/P1 → P2 → P3/P4 | `docs/audit_report.md` 严重程度分布与修复顺序 | ✅ |
| 禁止大面积重写 / 换技术栈 / 无理由升级依赖 / 删测试 / mock 核心逻辑 | 全程最小改动 + 复用既有架构；依赖仅做一次有 A/B 证据的兼容对齐 | ✅ |
| BUG / PARTIAL / MISSING / DEAD CODE / FAKE FEATURE / TECH DEBT 分类 | `docs/audit_report.md` 分类表 | ✅ |
| 最终报告（Bug 总数/修复数/P0..P4/修复文件/新增文件/删除文件/功能完成度/测试结果/完整链路/仍存问题） | `docs/audit_report.md`（全部小节） | ✅ |
| 禁止虚构测试结果、性能指标、Evaluation 数字或已实现功能 | 所有数字来自脚本生成或命令输出；离线 / 真实模型 / 脚本模型三种口径均明确标注 | ✅ |

---

## 唯一未完全验证项（外部运行时阻塞）

| 项 | 现状 | 解除条件 |
| --- | --- | --- |
| `docker build` + `docker compose up` 实机执行 | 本机无任何容器运行时（`docker`/`podman`/`buildah`/`nerdctl` 均缺失，WSL 无发行版）；已用真实 compose 文件的**无引擎拓扑验证**（`scripts/compose_smoke.py`）与 Docker 资产测试覆盖其等价语义 | 在任意一台装有 Docker 的机器上执行 `docker compose up --build`，并 `curl localhost:8000/health`（期望 `mcp_transport: http`） |

其余 24 个阶段、全部编号要求与交付物均已有可复现证据；该行是唯一需要外部环境的动作。
