# Windows 一键使用交付与验收记录

本页保留 9 月的历史交付证据。2026-10-05 个人资料研究版本、EXE 和当前验收范围见[个人研究交付记录](personal_research_delivery.md)。

日期：2026-09-28。状态：**实现及本机成品验证完成，最终产品验收未完成**。

本次更新 EXE 为 `dist/new/ResearchPilot.exe`，大小 49,533,455 字节（约 49.5 MB）。SHA-256：`92b2c7c239b2d992333fbfbe2dbe1472ec2b4fccbf3087fd9571f8420e37594e`。本地构建身份记录为 `output/desktop-build.json`。旧版 `dist/ResearchPilot.exe` 已于 2026-09-29 确认没有活动研究任务后正常退出并删除。

## 交付

- 单文件 `dist/ResearchPilot.exe`，Windows x64，无控制台；包含 Python、依赖、前端、内置语料和分词资源。
- 一键入口自动建目录、初始化资料、选择本机端口、启动服务、等待就绪、打开浏览器；重复启动打开已有实例。
- 系统托盘提供打开与退出。浏览器关闭不结束任务；前端也有退出入口。
- 前端首次设置、连接测试与保存、替换及清除 Key；DeepSeek / OpenAI / 自定义兼容服务预设。
- Windows 当前用户 DPAPI 保存 Key，前端、报告和诊断不返回密钥；桌面不读取 `.env`。
- 文件选择导入 TXT / Markdown / CSV / JSON / JSONL，中文编码兼容 UTF-8 / GB18030；重建基于持久化资料。
- 报告复制与下载、引用定位与原文、历史恢复、脱敏诊断、中文启动失败窗口。
- [构建脚本](../scripts/build_windows.py)、[成品冒烟脚本](../scripts/desktop_smoke.py)、[Windows 构建工作流](../.github/workflows/windows-desktop.yml)、[维护者说明](windows_build.md)。

数据保存到 `%LOCALAPPDATA%\ResearchPilot`。正常使用没有终端、手工安装依赖或配置文件编辑步骤。高级参数默认折叠。桌面研究明确使用内置或导入资料；内置网页样本不描述为实时搜索，也不把样本地址作为已联网验证的外部来源。

## 已获得的证据

| 范围 | 结果与边界 |
| --- | --- |
| 桌面配置与接口 | 自动测试覆盖无 Key 启动、禁止 mock 回退、无效 Key、保存失败保留旧配置、清除、重启、任务期间冲突、Origin/Host、密钥不回显 |
| DeepSeek 适配 | 请求体检查通过：`deepseek-flash`、关闭思考、JSON 模式；仍沿用 Pydantic 校验与修复 |
| 错误分类 | 密钥、余额、限流、模型不可用、网络与超时的中文提示，厂商响应不会原样暴露 |
| 资料 | 五种格式接口测试通过；导入、重建、重启、删除后一致；前端 UTF-8 / GB18030 解码测试通过 |
| 浏览器文件上传 | **PASS**：Playwright `setInputFiles()` 上传 `tests/fixtures/kb/retrieval.md`；页面显示 `retrieval.md`，`POST /kb/import` 返回 200，后端保存 1 篇、2 个片段，页面检索命中该文档；控制台错误 0 条。未使用 Windows 原生文件选择窗口 |
| 本机 EXE | 实际 PyInstaller 成品在源码目录外运行，中文及空格路径、受限 PATH、端口冲突、单实例、加密配置、重启和退出检查通过 |
| 托盘 | 成品启动包含原生 Windows 托盘，前端退出后进程释放；菜单的人工操作仍需最终验收 |
| 浏览器 | 无 Key 引导、无效测试 Key、测试连接保存后进入首页、提交研究、报告与引用、历史和诊断检查；使用本地兼容模拟服务 |
| 回归 | Python、前端、静态检查、类型检查与离线评测的最终执行结果见下方验证记录 |

最新成品检查结果保存在本地 `output/desktop-smoke.json`。本地兼容服务响应明确注明用于链路测试，不冒充真实研究。

## 仍未验收

| 项目 | 原因 |
| --- | --- |
| 干净 Windows 10 / 11 环境 | 当前机器有开发工具；隔离 PATH 的测试不能代替干净虚拟机 |
| 真实 DeepSeek Key 及真实研究质量 | 未通过 UI 输入真实 Key；没有使用、读取或生成用户凭据 |
| 完全非程序员人工首次使用 | 需要真实用户在上述环境完成研究；程序化测试不能代替 |
| 远端 Windows CI | 本机验收记录不代替远端构建；以 GitHub Actions 中该提交的 Windows 工作流结果为准 |
| 默认浏览器自动弹出、托盘菜单人工点击、Windows 10 | 入口已实现；本机脚本避免自动打开用户默认浏览器；需要最终环境人工核验 |
| 浏览器下载文件落盘 | 原生下载事件未在本次内嵌浏览器测试中确认；报告导出内容及范围标识通过前端测试 |

以上缺口意味着仍不能声称达到“非程序员双击后输入真实 Key 完成第一次研究”的最终验收标准。

## 验证记录

| 检查 | 本次结果 |
| --- | --- |
| 完整 Python 回归 `pytest -ra` | **433 passed、1 skipped**，75.01 秒；跳过的是原有容器拓扑测试（检测到容器工具时要求改用容器启动），没有宣称真实 Docker 验收 |
| 前端 `node --test tests/frontend/app.test.mjs` | **27 / 27**，含中文解码、同源请求、密钥清空、保存默认值、引用绑定及导出资料范围 |
| Ruff check / format | 通过，151 个 Python 文件格式检查；本机未尝试修改原有无关测试的类型错误 |
| Mypy | 核心包及新增维护脚本通过（80 个文件）；`mypy .` 包含原有测试及忽略的临时脚本，会出现既有类型错误，不声称全仓库类型检查通过 |
| pip check | 无依赖冲突 |
| 完整离线评测 | **35 / 35**，引用正确性 100%，检索召回 93.89%；仅确定性 mock，不能代表真实模型质量 |
| wheel | 构建成功，安装到第二个环境，从源码外执行 CLI / API / MCP / 研究冒烟通过 |
| EXE | 新版 `dist/new/ResearchPilot.exe` 单文件无控制台构建成功；通过 `scripts/desktop_smoke.py`，并确认文件名反馈已包含在打包资源中 |

本地浏览器检查截图位于 `output/desktop-citations.png`、`output/desktop-settings.png` 和 `output/playwright/desktop-file-import-pass.png`。本轮使用仓库内的 `tests/fixtures/kb/retrieval.md` 测试浏览器文件上传；`setInputFiles()` 成功。截图和浏览器日志属于本地验证产物，不进入 Git。
