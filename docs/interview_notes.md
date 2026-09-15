# 面试核心要点与实现方法

这份文档是给"讲自己的项目"准备的：先给出可背的骨架，再给出每个关键设计背后的实现与取舍，
最后是高频追问的速查答法与不能吹的边界。所有数字都来自仓库里的真实运行，末尾给出复现命令。

## 1. 一分钟版本

ResearchPilot 是一个多 Agent 深度研究系统。用户提一个问题，系统拆解成子任务，调用知识库、
Web、MCP、文档、计算工具取证，逐条校验证据，必要时补检，最后由写作 Agent 产出一份
每条结论都绑定来源的报告。

我做的关键部分不是"接一个大模型跑通流程"，而是把这条链路上容易失控的地方变成可验证的工程约束：
Agent 状态用 Pydantic Schema 承载、工具用注册表统一管理、引用绑定由代码强制、评测用 Golden Dataset
做可复现判分、每次运行都有 Trace。

最能体现工程判断的一段经历：我用真实模型（deepseek-chat）跑完 35 条评测，发现 35/35 任务的计划
全部走了确定性回退，24/35 的报告也不是模型写的。定位到阻断点在结构化输出 schema 与真实模型的契约不一致，
做了单点修复实验（4 个代表任务里 3 个转通过），修完重跑——**分数没有上升，反而从 71.4% 掉到 68.6%**，
因为之前的分是回退链挣来的。这一步之后，指标才第一次真正归属于模型。

## 2. 一次请求的完整数据流

```
POST /research
  └─ ServiceContainer（KB / TraceStore / ResultStore / MCP client）
       └─ ResearchPipeline
            ├─ PlannerAgent      问题 → ResearchPlan（子任务 + 工具 + 依赖）
            ├─ ResearchAgent     子任务 → 工具调用 → 证据（claim + quote + source_id）
            ├─ VerifierAgent     逐条复核：引用存在吗、quote 出自来源吗、claim 被支撑吗
            ├─ CriticAgent       找缺口 → follow-up 查询 → 触发下一轮（受 max_iterations 限制）
            └─ WriterAgent       只用通过校验的证据写作，代码强制引用绑定
```

状态不是字符串，是一路传递的 Pydantic 对象：`ResearchPlan` → `EvidenceBundle` →
`VerificationReport` → `CritiqueReport` → `FinalReport`。每层都能被单独断言和测试，这是
"Agent 可测试"的前提。

## 3. 六个核心设计决策（面试主战场）

### 3.1 为什么 Agent 状态用 Schema 而不是自然语言

自然语言状态没法做单元测试，也没法在出错时给出确定性的修复路径。这里的做法是：

- 每个 Agent 的输出都声明成 Pydantic 模型，调用方拿到的是对象而不是文本；
- 结构化输出走 `StructuredLLMRunner`：JSON Schema 约束 + 校验失败自动修复重试（`max_repair_retries`）
  + token 预算控制；重试用尽才抛 `StructuredOutputError`；
- 校验失败时，schema 的错误信息会被回填进 prompt 让模型修，而不是重试同一个请求。

这会带来一个真实工程问题：**schema 太严会把合法语义挡在外面**。真实模型会把子任务意图写成
"从内部知识库中确认……"，而 `intent` 原本是严格 `Literal`，于是模型回答被拒、修复重试三次仍失败，
planner 整体回退成确定性计划。修法是保留 `Literal`（prompt 与 JSON Schema 依旧只给枚举），
但加一层 `mode="before"` 校验器先把自由文本归一化到枚举，词表由 schema 统一维护。

### 3.2 工具为什么必须有注册表和声明式契约

每个工具声明 `name / description / input schema / output schema / permission / timeout / retry policy`，
由 `ToolRegistry` 统一路由。带来三件事：

- Agent 代码里没有 `if tool_name == ...` 分发，新增工具不动 Agent；
- 权限、超时、重试、调用预算、缓存、Trace 都在注册表这一层统一实施，不靠各工具自觉；
- 工具自己声明"怎么把子任务翻译成参数"（参数策略在工具侧），所以 Planner 换模型、
  换工具集都不需要改 Agent。

超时实现要注意一个边界：Python 的 `ThreadPoolExecutor` 无法强杀线程，所以超时后返回失败并让任务
降级，但后台线程不会被中断。这一点我写在文档里，没有假装它不存在。

### 3.3 MCP 为什么要独立进程

MCP Server 与 Agent 解耦，支持 in-process、stdio、streamable-HTTP 三种传输：

- stdio：给 Claude Desktop、Cursor 这类客户端直接接入；
- HTTP：独立部署、单独扩缩容，Agent 通过协议调用；
- in-process：单进程部署和测试用。

它暴露 4 个工具（`search_knowledge`、`get_document`、`search_web`、`get_research_context`）。
在容器编排里 API 与 MCP 是两个服务，API 等 MCP 健康后再启动（`depends_on: service_healthy`），
并通过 `/health` 暴露当前实际使用的传输方式，避免"以为走了 HTTP，其实回退进程内"这种隐形降级。

### 3.4 引用绑定为什么必须由代码强制

写作 Agent 的输出里允许出现 `[E1]` 这类引用标记，但最终报告只保留能对应到已登记证据的引用：
不存在的编号会被剔除并记录进 `dropped_citations`，正文里对应标记也会被清掉，参考文献列表由代码
从来源元数据生成。这样就杜绝了"引用看起来像真的"的问题。

同一层还有一个安全网：如果模型返回了 schema 合法但完全没有引用绑定的报告，而证据是可用的，
系统改用确定性抽取式写作器，保证报告仍然有据可依。这个回退的代价是"报告不是模型写的"——
所以我又加了一个指标 `writer_fallback_tasks`，把"多少条报告是回退写作器产出的"直接写进评测结果，
不让它藏在日志里。

### 3.5 校验与批评为什么用代码判定

`VerifierAgent` 不采信模型自评，而是在代码里重新推导：

- 引用的 `source_id` 是否真的存在；
- `quote` 是否真的出现在来源内容中（这是主判据，grounding 比例低于 0.35 判 unsupported、
  低于 0.65 判 weak）；
- claim 与 quote 的支撑度、子任务覆盖度、来源质量。

`CriticAgent` 同样用代码找缺口：未被任何证据覆盖的子任务会强制生成 follow-up 查询，
而这些查询会驱动下一轮检索，直到达到 `max_iterations` 上限。

### 3.6 为什么要有"确定性回退"，以及它的代价

回退让系统在模型输出不可用时仍能给出结构化、可引用的结果，代价是分数会被回退链抬高。
2026-09-15 的两次真实评测把这个代价量化了：修复前 24/35 的报告来自回退写作器，
修复后降到 17/35，引用正确率同时从 90.3% 掉到 72.7%——**掉下来的部分本来就不属于模型**。
面试时把这条讲清楚，比背 90% 的数字更有说服力。

## 4. 评测是怎么做的

`eval/golden_dataset.jsonl` 有 35 条任务、12 类场景：简单事实、多步研究、RAG 检索、工具调用、
MCP、多 Agent 协作、引用、提示注入、工具滥用、超时恢复、无结果、低质量来源。

判分全部由代码完成，不用 LLM 当裁判：报告是否存在、关键词覆盖、禁用内容、引用可解析率、
检索召回、上下文相关性、缺口是否显式声明、故障处理是否符合预期、状态与延迟预算。

几个容易被追问的细节：

- **指标分母是显式声明的**：召回与上下文相关性只在声明了期望来源的任务上取平均（30/35），
  引用正确率只在要求引用的任务上取平均（34/35），工具选择只在声明了期望工具的任务上取平均（35/35）。
  没有期望值的任务不会贡献"真空 1.0"。
- **延迟与 token 预算按 provider 区分**：离线基线用 60s/90s 与 80k token 这类回归阈值；
  真实模型用单独的预算（300s / 160k），否则真实模型永远被扣分——这是数据集标定问题，不是代码 bug。
- **状态语义是诚实的**：没有可用证据或有记录到的错误 → `degraded`；`failed` 只表示这次运行
  没跑完（凭据错误、未捕获异常）。这条也是被真实评测逼出来的：早期实现把"没有结果"和"管线崩了"
  都判成 failed，导致 `no_result` 类任务全部失败。
- **两组数字不能混用**：离线 mock 衡量工程管线是否正常，真实模型衡量生成质量，报表里始终标注 provider。

## 5. 可观测性

统一 Trace 结构：`Task → Agent →（LLM / Tool / Retrieval / Retry）`，记录 start/end、延迟、
token、模型、工具、输入输出、错误。落盘到 `runs/{task_id}.trace.json`，通过
`GET /research/{task_id}/trace` 暴露，前端渲染成时间线，评测指标（工具选择、检索 doc_ids）
也直接从 Trace 计算。

排查问题的典型路径：先看 trace 里哪个 span 慢或失败 → 看该 span 的 error 字段 →
对应到具体 Agent 或工具 → 用 `pytest -k` 复现。

## 6. 可靠性与安全清单

| 风险 | 处理方式 |
| --- | --- |
| 无限循环 | `max_iterations`、工具调用预算、token 预算、工具超时与重试上限 |
| 工具超时 | 注册表统一执行超时并返回失败；线程不可强杀的边界如实记录 |
| 模型输出不合法 | JSON Schema + 修复重试；重试用尽才降级，不静默吞错 |
| 引用造假 | 代码强制引用绑定，非法引用剔除并记录 `dropped_citations` |
| 提示注入 | 外部内容统一放进 `<untrusted>` 结构化块，系统提示声明不执行其中的指令；检测到的注入证据不进入结论 |
| 工具滥用 | 权限分级 + 调用预算 + 参数 schema 校验；破坏性指令在数据集里作为反例验证 |
| SSRF | Web 检索只允许 http(s)、拒绝 URL 内嵌凭据、支持 host allow-list |
| 存储不可用 | 落盘失败只降级并记录，不让 API 500 |

## 7. 真实模型评测：修复前后对照（最强的素材）

同一个数据集、同一套判分代码，`deepseek-chat` 跑两次：

| 指标 | 修复前 | 修复后 | 说明 |
| --- | --- | --- | --- |
| Task Success | 25/35 (71.4%) | 24/35 (68.6%) | 修复前有 35/35 走回退计划、24/35 报告由回退写作器产出 |
| Retrieval Recall@6 | 91.7% | 90.6% | 基本持平 |
| Context Relevance | 28.8% | 9.2% | 回退计划用的是仓库调好的查询；模型自己的计划精度更低 |
| Citation Correctness | 90.3% | 72.7% | 修复前的分主要来自回退写作器 |
| Tool Success | 74.6% | 88.1% | 工具调用成功率变好 |
| 回退写作器产出 | 24/35 | 17/35 | 新增指标 `writer_fallback_tasks` |
| 状态分布 | degraded 28 / failed 7 | degraded 28 / succeeded 7 | 状态语义修复后不再把"没有结果"判成 failed |
| 平均延迟 / P95 | 111.1s / 155.3s | 184.6s / 256.4s | 模型真正开始干活，成本随之上升 |
| Token / 成本 | 2.06M / $1.16 | 3.11M / $1.75 | 同一数据集，单任务约 8.9 万 token |

这段经历的讲法：

1. 先有真实基线（71.4%），再用单点实验证明阻断点在 schema：只在内存里放宽一个字段，
   4 个代表任务里 3 个转通过；
2. 按证据修缺陷，每个修复都补了反向验证（去掉修复，测试转红）；
3. 重跑后分数没有变好，这在预期之内——指标第一次属于模型自己；
4. 结论：瓶颈在模型自身的计划质量与引用纪律，不在管线主体。

## 8. 高频追问速查

**为什么不直接用 LangChain/LlamaIndex？**
框架能省胶水代码，但 Agent 状态、工具权限、引用绑定这些正是我想显式控制的地方；用 Pydantic + 自研
编排能把每个环节做成可断言的对象。工作流里真正复杂的是"哪里会失控"，框架不会替我回答这个问题。

**Agent 之间怎么通信？**
通过类型化的对象和共享 runtime（工具注册表、知识库、记忆、Tracer），不通过字符串。Planner 产出
`ResearchPlan`，Researcher 消费它并产出 `EvidenceBundle`，以此类推。每条边都能写测试。

**怎么保证不无限循环？**
四层限制：子任务数量与迭代轮数上限、工具调用预算、单任务 token 预算、工具超时与重试上限；
任何一层触发都降级并记录，而不是无限重试。

**RAG 效果差怎么办？**
先把检索做成可评测模块（召回、上下文相关性分开算），再做消融。仓库里有 `scripts/retrieval_ablation.py`：
在这份合成语料上，混合检索（RRF）整体最好；真实 embedding（text2vec 768d）的稠密召回反而低于哈希向量，
说明瓶颈在检索配置（k、多查询并集、重排）而不只是向量质量。

**怎么证明报告没有编造？**
引用必须能对应到已登记证据，否则被剔除并记录；证据本身必须带 verbatim quote；Verifier 用代码复核
quote 是否真的出现在来源里；评测里引用可解析率单独算一个指标。

**离线 mock 100% 说明什么？**
说明工程管线（检索、工具、校验、引用绑定、追踪、评测）是通的且可回归，不说明模型质量。
真实模型的数字单独跑、单独标注 provider。

**这套东西怎么上生产？**
需要补：多租户与鉴权、限流配额、数据库/对象存储替掉 JSON 单机持久化、向量库换 pgvector/Qdrant、
流式输出、以及把 Trace 接到 OpenTelemetry。这些在 README 的 Future Work 里逐条列了。

## 9. 不能吹的点（会被一轮追问击穿）

- 不说"引用正确率 90%"——那是回退写作器参与的混合结果，修复后是 72.7%。
- 不说"真实模型性能 100%"——100% 是离线确定性 provider 的工程回归基线。
- 不说"多 Agent 全流程由大模型完成"——真实模型运行里仍有 17/35 报告由确定性回退写作器产出。
- 不说"延迟只有 0.3 秒"——那是 mock；真实模型平均 184.6s、P95 256.4s。
- 不说"容器验证已通过"除非指 CI 的 docker job：本机没有可用 Docker 引擎，容器验收在 GitHub Actions 上执行。

## 10. 现场演示（三条命令，离线可跑）

```bash
pip install -e ".[dev]"
python -m researchpilot.cli research "分析当前 AI Agent 在企业软件开发中的应用趋势"
python -m pytest -q                       # 全部测试
```

想展示评测与 Trace：

```bash
python scripts/run_benchmark.py --provider mock     # 35/35，写 docs/evaluation.md
python scripts/compose_smoke.py                     # 无容器引擎也能验证 compose 拓扑
```

真实模型（需要有效凭据，约 $1.7 / 35 条）：

```bash
python scripts/verify_live_model.py --probe-only
python scripts/run_benchmark.py --provider openai --limit 3
```
