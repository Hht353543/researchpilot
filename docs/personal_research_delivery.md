# 个人资料研究助手实施与验收记录

2026-10-05。状态：本轮工程回归、真实模型实验、本机 EXE 验证与最终验收准备完成；产品最终验收仍待完成。用户确认暂时没有干净 Windows 环境和独立测试者，本轮先完成真实模型实验与验收准备。人工语义评分、账单核对和独立用户全流程仍保持待验收状态。

## 本轮交付

新版 Windows x64 单文件为 [ResearchPilot.exe](../dist/personal/ResearchPilot.exe)，51,588,442 字节。SHA-256：`b24ff926587ec977126314cd5d2b67d2647d8aae9580456a6b7f73212d910453`。此前的 `dist/new/ResearchPilot.exe` 保留为旧版本。成品与本机测试输出不进入 Git；构建记录为 `output/personal-build.json`。

| 路线 | 已完成行为 |
| --- | --- |
| 个人定位 | 首页、资料页、连接说明和使用文档统一面向个人资料研究。桌面隐藏开发评测和底层参数入口，连接处提供密钥获取、计费和资料发送说明。 |
| 可信依据 | 原文须存在于来源；词语重合不能证明支撑。完整原句可直接核对，转述必须消费语义审查结果。否定、数字、单位与日期有保守否决；漏判或未审查的转述不当作有依据。 |
| 最终报告 | 核对生成后的结论、分析正文和摘要；不能确认的文字退回原文摘录或标明证据不足。建议标为推断，正文不展示原始置信度与证据 ID。 |
| 资料范围 | 本次研究保存选定的文档 ID，以隔离的资料快照执行检索与原文读取。桌面正式研究不调用 Web/MCP 检索，不混入其他文档。 |
| 文件导入 | 支持文字 PDF 页码、DOCX 正文及表格；复用 pypdf/python-docx。扫描 PDF 明确提示 OCR。桌面请求上限 8 MiB、正文上限 50 万字符；PDF/DOCX 原文件需小于约 6 MiB。 |
| 独立示例 | 新安装从空资料库开始；主动载入示例后进入独立范围。旧版自动种入的、内容未修改的示例会标记迁移，正式研究默认排除。 |
| 成本与进度 | 直接摘录跳过不必要的模型审查；补检只跑缺口，保留冲突证据；移除未被 Provider 消费的长期记忆读取。进度由实际执行阶段发布，并遵守持久任务的归属与终态检查。 |
| 继续追问 | 继承上次报告、证据和资料范围，将它们作为待核对上下文交给规划，再检索当前资料。资料被删除或更换范围时要求新建研究。 |
| 可交付报告 | 引用显示来源编号、点击定位并展开原文；HTML 可独立打开，Markdown 保留核对附录，打印自动展开原文并恢复页面状态。渲染复用本地 markdown-it/DOMPurify，保留许可证。 |

文本解析参考 [pypdf 官方说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html) 与 [python-docx 官方说明](https://python-docx.readthedocs.io/en/latest/user/documents.html)。本轮未增加 OCR、实时联网、外部 MCP、团队协作或新的向量方案，也未重写整个前端或持久化模块。CLI、mock、Trace 与现有维护者工具继续保留。

## 验证证据

| 检查 | 结果与范围 |
| --- | --- |
| Python 全量回归 | 460 通过、1 跳过，102.90 秒；总收集数 461。Compose 相关测试按环境条件跳过；不计作已验证。 |
| 前端回归 | Node 32 项通过；包含资料范围、继承追问、二进制编码、来源编号与实际阶段。 |
| 静态检查 | Ruff 检查通过、154 个 Python 文件格式检查通过；mypy 80 个源码文件通过。 |
| 可信度定向回归 | 否定反转、数字/日期替换、年份对应关系、冲突与无答案、缺失语义结果、伪造原文及报告改写回退。结构完整但不受支持的引用不能得到“引用正确”成绩。 |
| API 与存储 | PDF/DOCX 二进制导入、重建与重启保留；包括带附件、超过旧请求上限的 PDF。示例标记经资料列表保留，正式范围不能选入示例。已有取消、超时、跨进程恢复与凭据脱敏回归通过。 |
| 真实浏览器 | Edge 隔离配置中执行连接、PDF/DOCX 导入、勾选一份资料、生成报告、核对否定原文、继续追问、切换示例并返回个人资料。追问确有父任务 ID 与相同文档范围。模型端点为本地脚本服务。 |
| 渲染和导出 | 真实 DOMPurify 移除脚本与事件属性，Markdown 拒绝可执行链接。浏览器下载 HTML；PDF 导出为 2 页，包含资料范围与引用原文。打印前展开、打印后恢复已验证。 |
| Windows 成品 | 最终 EXE 的 11 项独立 smoke 检查通过：隔离启动与端口回退、前端与空资料库、DPAPI、热配置、PDF/DOCX、引用、追问、示例隔离、单实例、重启历史、原生托盘与退出。该脚本使用本地 OpenAI 兼容服务。一次并行检查在托盘退出时超时，独立重试通过，原因未确认，人工托盘验收仍待执行。 |
| 真实模型成品链路 | 同一最终 EXE 使用用户保存的 DeepSeek 连接，完成日期题及继承范围的追问。两题均 completed、errors 为空、quality 为 degraded；具体遗漏与重复见下节，不计作人工通过。 |

本机证据：`output/personal-phase-delivery-pytest.xml`、`output/personal-desktop-smoke-delivery.json`、`output/playwright/goal-setup.png`、`output/playwright/goal-report.png`、`output/playwright/personal-report.pdf`。既有脚本模型浏览器实验使用 `output/goal-ui-data`；本轮真实模型 EXE 使用 `output/personal-acceptance-app`。均为隔离目录，没有修改正式用户数据。

维护者在安装项目开发依赖后可运行：

```powershell
python -m pytest
node --test tests/frontend/app.test.mjs
python -m ruff check researchpilot scripts tests
python -m ruff format --check researchpilot scripts tests
python -m mypy researchpilot scripts/build_windows.py scripts/personal_acceptance.py --no-incremental
python -m scripts.build_windows --dist-dir dist/personal
python scripts/desktop_smoke.py --exe dist/personal/ResearchPilot.exe --json-out output/personal-desktop-smoke-delivery.json
```

打包使用仅含核心依赖与桌面构建依赖的隔离 Python 环境，避免把本机可选的模型训练依赖带入成品。jieba 的上游 `pkg_resources` 弃用警告仍存在，当前回归和成品启动通过。

## 本轮真实模型实验

用户指定 DeepSeek-V4.1-Flash，使用官方 API 名称 `deepseek-flash`；截至本次实验，官方文档确认该映射。连接 Key 由用户在隔离桌面应用中保存，实验复用现有 DPAPI 配置，不在命令或记录中输出密钥。[DeepSeek 官方说明](https://api-docs.deepseek.com/)

复用现有 Provider、知识库和 Pipeline，新增 [维护者执行器](../scripts/personal_acceptance.py) 与 [六类合成资料](../eval/personal_acceptance.json)：否定、数字与单位、日期对应关系、必要条件、资料冲突及无答案。基线为提交 `02b4ab227af942a266e7b4cd5ce65b3d9628c550`；`output/goal-baseline` 的 84 个研究代码文件经 Git blob 哈希核对，与该提交一致。

旧、新版统一使用官方模型、相同资料和问题、hash embedding、`top_k=4`、`max_iterations=1`、`temperature=0.1`、`max_tokens=1200`、单题 30,000 token 预算，只允许资料检索、原文读取和元数据工具。每题独立知识库使旧版也只接触该题资料；旧版没有桌面选定范围和父任务字段，不能据此声称旧版桌面隔离或追问已通过。

### 实验发现与修复

1. 首轮真实模型暴露 JSON mode 只保证 JSON 语法、没有完整字段契约：Planner 返回 `goal` 等字段导致计划回退。复用 Pydantic 的 JSON Schema 注入系统提示；简单事实题允许一个集中子任务。官方也要求在 JSON mode 提示中说明输出格式。[JSON mode 文档](https://api-docs.deepseek.com/guides/json_mode/)
2. 报告审查把证据 ID `E3` 返回为待审文字 ID，导致正确文字被当作未审查。提示明确 `checks[].evidence_id` 必须匹配输入项的 `id`，并给出 `C0`、`S0` 与 `summary` 示例。
3. 资料清理已做 NFKC 规范化，核对端未同步，中文逗号与 ASCII 逗号差异误拒原文。核对端采用同样的规范化，仍保留完整条件句和否定保护，并增加回归。
4. 写作提示要求合并重复证据、直接回答问题，减少内部核对术语；实际成品仍存在重复，保留为验收问题。价格表补入官方峰时、缓存未命中估算，不再用旧版的零费用字段解释实际计费。

### 同资料真实模型对照

详细数据、源码与资料哈希、逐题记录见 [真实模型对照数据](personal_live_comparison.json)。下表只比较六个初始问题；新版追问单列，不混入比较分母。调用数和 tokens 来自实际成功 HTTP 响应的 usage，包含返回 JSON 后未通过结构校验的响应；Pipeline 内部 metrics 可能未计入这类响应的用量。

| 指标 | 旧版基线 | 修复前新版 | 修复 schema 后中间版 | 最终新版 |
| --- | ---: | ---: | ---: | ---: |
| 初始问题数 | 6 | 6 | 6 | 6 |
| 成功模型 HTTP 响应数 | 67 | 95 | 45 | 49 |
| 实际 tokens | 99,030 | 171,026 | 96,490 | 112,865 |
| 单题平均运行耗时 | 36.149 秒 | 51.703 秒 | 24.457 秒 | 26.153 秒 |
| 人工语义完全通过题目 | 待评审 | 待评审 | 待评审 | 待评审 |

最终新版相对旧版调用减少 26.9%，但 tokens 增加 14.0%；不能声称全面节省成本或提高质量。最终六题和一条否定追问共 58 次成功响应、132,093 tokens，自动质量标签全部为 degraded。否定、数字、条件、冲突和无答案题的初审观察已进入验收表，但没有替代人工评分。日期题在 30,000 token 预算下触发 `report_verifier: BudgetExceededError`，退回摘录后摘要漏答上线日期；否定追问还记录了一条未知来源 `metadata` 的生成证据被丢弃。逐题错误保存在对照 JSON。预算按已消费用量及下一次调用额度检查，不能当作严格费用上限。

账户余额与变动记录属于私人数据，不纳入公开交付。旧版只含六题，新版各含额外追问。缓存、时段、执行顺序、采样与同账户其他活动未控制，实际费用须在本地以用量明细核对，不能直接当作同题成本降幅。表内耗时为单次本机观测，不是性能保证。

### 最终 EXE 默认预算补核

最终 EXE 使用实际保存配置：`top_k=6`、`max_iterations=2`、`temperature=0.2`、`max_tokens=1200`、预算 160,000 tokens。日期初始题为 19 次模型调用、38,301 tokens、54.426 秒；继承追问为 18 次调用、39,486 tokens、52.635 秒（此处为 Pipeline metrics）。两题无运行错误，父任务与资料 ID 一致，初始摘要保留 2025 年 10 万元、2026 年 20 万元及 2026-09-10 上线三项事实。

追问正文保留上线日期，但摘要漏了日期，收入变化段落退回原始数字，重复摘录及重复引用仍明显；限制说明还包含需要人工判断的“时间线矛盾／口径张力”等推断。两题仍为 degraded，不能计为完整答案通过。该补核参数不同，未合并进上面的六题对照。

本机原始证据：`output/personal-live/{before,after,after-schema,final}/experiment.json` 及各题 JSON／Markdown；成品证据为 `output/personal-live/exe-check.json`、`exe-initial.json`／`.md`、`exe-followup.json`／`.md`。界面已打开实际日期报告供核对。原始输出留在本机，精简对照 JSON 与验收清单进入文档。

## 已有同资料、同 mock 模型对照

此历史记录发生在本轮真实模型修复之前。基线为同一提交；当时新版工作区使用相同的 35 题开发数据集、知识库、离线网页样本、hash embedding、`top_k=6`、`max_iterations=3` 和确定性 mock Provider，各运行一次。详细数字保存在[对照数据](personal_mock_comparison.json)，不能当作最终源码的新一次实验。

| 指标 | 基线 | 当时新版 |
| --- | ---: | ---: |
| 自动验收题目 | 35/35 | 35/35 |
| 模型调用 | 309 | 253 |
| 工具调用 | 175 | 104 |
| 模拟 tokens | 700,754 | 568,676 |
| 单题平均本机耗时 | 0.548 秒 | 0.430 秒 |
| 检索召回 | 93.89% | 93.89% |

这只能证明固定 mock 场景下调用减少且既有自动契约通过。耗时是单次本机观测，tokens 是模拟用量，费用为零。旧、新“引用正确率”口径不同，且适用题目由 34 变为 32；不能把两次 100% 当作真实语义质量提升。开发数据集还包括旧 MCP/离线 Web 工具场景，桌面个人资料范围另有定向与成品测试。

## 最终验收准备与剩余事项

已准备 [最终验收表](personal_acceptance_review.md)，包含固定资料、执行命令、预期答案、逐句核对方式、旧新版评分表及干净 Windows 全流程。所有人工结论均留空待评审。后续仍需：

1. 没有参与开发的人在干净 Windows 10/11 上，用自己的真实资料独立完成连接、导入、选择、提问、核对、导出、托盘退出与重启。
2. 人工评审真实模型的六类案例和追问，记录正确结论、遗漏、误报及不受支持的推断；重点核对预算回退、摘要完整性和重复内容。自动核对与原文摘录不能代替完整上下文审查。
3. 按同题分母核对旧、新版人工质量和账户用量明细；必要时控制缓存／时段并重复实验，再决定是否调整 Agent 步骤。

本轮用户指定的“真实模型实验与验收准备”已完成。当前证据不等于干净 Windows 或非开发者验收；只有上述证据补齐并关闭影响验收的问题后，才能将产品状态改为最终验收完成。
