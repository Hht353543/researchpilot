# 最终验收问题修复与复验记录

日期：2026-10-06。执行者：Codex。对应[原验收失败报告](final_acceptance_20261006.md)中的 A01–A05。原报告和失败产物保留，本记录不补填独立用户、干净 Windows 环境或人工账单验收签字。

当前候选成品：`dist/acceptance-reviewed/ResearchPilot.exe`，51,595,174 字节，SHA-256：`7d9f1896d56132683ed9ea8546f2f2505ba43a7e824cd864756a25b4b4629658`。原 `dist/personal/ResearchPilot.exe` 保留。

## 修改与关闭依据

| 问题 | 修改 | 复验依据 |
| --- | --- | --- |
| A01 / P1：错误单位缺失与时间矛盾 | 复核结论、正文、摘要之外，增加限制说明和建议事实前提的复核，并提供完整来源上下文。原文已经给出单位时拒绝“未提供单位”；收入与上线时间本身不能证明矛盾。被拒绝的断言不再搬进限制说明。 | 回归覆盖复核模型错误批准的情形；真实日期题及重复题不再输出单位缺失或无依据时间矛盾。合理的统计口径缺口仍可保留。 |
| A02 / P1：已发生用量漏记 | 收到成功响应后，在 finally 中记录完整 usage、结束 span，并保留预算/取消错误。调用前检查已耗尽预算，避免后续步骤继续请求模型。 | 结构化与文本调用、跨预算、响应返回时取消均覆盖。最终 EXE 实测预算 1,000，唯一响应 1,281 tokens，metrics 与 LLM span 均为 1,281，span 关闭并带预算错误。[实测](../output/acceptance-fixes/live-budget-check.json)。 |
| A03 / P2：重复回退、摘要漏答、计算被删 | 复核展开后再次去重；已覆盖事实不再重复添加，摘要包含全部保留结论。文档分块和全文按 doc_id 合并参考编号、来源卡片与来源计数，保留不同原文。计算复用现有安全 AST 计算器，检查来源输入、公式与结果，同时保留语义复核和“推断”标签。 | 覆盖中英文标点、减号、乘除及百分比写法、错误结果与非来源输入；真实追问直接给出增加 10 万元、增长 100% 及上线日期。ISO 27001 标准名称不再误当数值；无答案题直接回答无法确认认证。 |
| A04 / P2：偶发退出超时 | 已定位为启动检查期间收到正常退出，被误判为启动失败；打包程序随后弹出错误窗口而留驻。增加明确的退出请求状态，区分正常退出与意外启动失败。 | 源码复现保存了启动异常栈；修复前立即退出多次返回异常。最新 EXE 20 次原生托盘启动/退出全部返回 0，instance.json 全部移除；意外服务停止仍报错。[连续复验](../output/acceptance-fixes/tray-final.json)。未操作人工托盘菜单。 |
| A05 / P3：导出资源依赖与来源跨页 | 独立 HTML 移除引用外部 sprite 的 SVG；导出和主页面打印样式均对来源卡片设置 break-inside: avoid。 | 浏览器实际下载 HTML，经独立静态服务打开，仅出现浏览器默认 favicon 请求，没有图标请求。两页 PDF 逐页检查，来源名称、元信息和两条原文完整处于第二页，未被拆开。[HTML](../output/acceptance-fixes/export/report.html)、[PDF](../output/playwright/acceptance-fixed-export.pdf)。 |

来源和引用正文保留原文，不为了去重改写引用。证据附录保留证据编号，所以多个编号可能指向同一文档；它们不再被展示为多个独立来源。打印仍可能有分页和留白，修复目标是保持来源卡片完整。

## 工程与成品验证

| 检查 | 结果 / 证据 |
| --- | --- |
| Python 全量 | 485 通过，1 跳过，共 486 项，96.193 秒；[JUnit](../output/acceptance-fixes/pytest-reviewed.xml)、[日志](../output/acceptance-fixes/pytest-reviewed.log)。跳过项为容器拓扑检查，不计容器运行验证。 |
| 前端 | 34/34；[日志](../output/acceptance-fixes/frontend-final.log)。 |
| Ruff / format / Mypy | 全部通过；154 个文件格式检查；核心包及两个维护脚本共 80 个文件类型检查。 |
| 隔离构建环境 | pip check 通过；使用已有隔离 PyInstaller 环境重建，没有引入新依赖。[构建日志](../output/acceptance-fixes/build-reviewed.log)。 |
| 最新 EXE 桌面检查 | 11 项通过，含受限 PATH、中文/空格路径、端口回退、DPAPI、热配置、PDF/DOCX、追问范围、示例隔离、单实例、重启和原生托盘退出。[结果](../output/acceptance-fixes/desktop-reviewed.json)。 |
| 成品与源码记录 | [release-manifest.json](../output/acceptance-fixes/release-manifest.json)。验收对象为保留已有未提交改动的工作区，不把 Git HEAD 当成当前成品源码。 |

## 真实模型复验

使用已有验收连接的 DeepSeek `deepseek-flash`，只处理合成资料。报告修复构建 `d42b743d…` 完成六类资料题、否定追问、收入追问及日期重复，共 9 题；均 completed、errors 为空，1 题 quality=succeeded，8 题 degraded。Codex 对原文和报告核心答案逐题核对，覆盖否定、数字/单位、条件、冲突、日期和无法确认认证；没有把 completed、模型自评或关键词检查直接当成人工质量成绩。[九题汇总](../output/acceptance-fixes/live-final/live-summary.json)。

随后仅补充预算调用前检查，形成当前候选 `7d9f1896…`。该候选再次执行日期、收入追问和无答案题，3 题均 completed / degraded，errors 为空；收入追问在结论和计算章节保留增加 10 万元、增长 100% 和上线日期，无答案题明确“无法确定”，没有把未提及当成未认证。结果见[当前候选汇总](../output/acceptance-fixes/live-reviewed/live-summary.json)。具体最终结论应连同原文核对，degraded 表示部分生成文字仍会被替换为原文或提示证据缺口；本次有限样例不证明模型在所有资料上均正确。

模型账户按实际服务计费。任务 usage 与 span 对账验证的是项目用量记录，项目价格表的成本估算不等于账单。独立人工账单核对仍未完成。一次响应可能跨过预算阈值，修复保证它被完整计入并阻止后续请求，预算不是对已经发生的单次响应进行退款或截断。

## 交付边界

这些修复完成后可作为重新验收候选。干净 Windows 10/11 环境、独立用户操作（含托盘菜单）和人工账单核对仍需要补齐；本机源码外成品检查不能替代这些证据。未部署、发布、推送或修改人工签字表。

代理复验结论：A01–A05 的代码修复及本机自动复验完成。项目最终人工验收仍待上述证据。测试进程、隔离浏览器和预览服务均已退出，临时复制的加密模型配置已移除，原验收连接保留。
