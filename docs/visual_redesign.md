# Web UI Visual Redesign Result

## 1. Previous Visual Problems

实际启动并体验旧界面后，最明显的问题是：导航只滚动到同一长页面；研究、知识库、设置同时争夺注意力；
历史常驻侧栏，挤压报告；标题、卡片与阴影重复，主次弱；知识库默认显示分块信息；报告来源列表顺序与正文
参考编号不一致；进度会从 running 状态推断已完成阶段；字体与窄窗口导航也缺乏稳定规则。

## 2. Design Direction

专业、安静的团队知识工作空间。暖灰导航、白色正文、低饱和青绿强调色。统一颜色、字号、间距、
控件/卡片/弹窗圆角、边框与 180ms 状态转换。保留 HTML/CSS/JS、现有 API 与离线使用能力。
参考 Linear 公布的界面统一与降低视觉噪声原则，未复制其完整设计。

## 3. External Assets Used

Lucide 0.468.0：23 个本地 SVG symbols，ISC 许可；上游版权说明包含 Feather-derived MIT 部分。
用于导航、品牌图标/favicon、操作、来源、文档和空状态。来源、许可与使用位置见 [visual_assets.md](visual_assets.md)，
原始许可随静态文件和 wheel 分发。没有增加字体托管、CDN 或运行时 npm 依赖。

## 4. Major UI Changes

| Area | Result |
| --- | --- |
| App Shell | 固定侧栏、当前页标识、工作区标题、系统状态；桌面窄窗用图标导航，手机用底部导航 |
| Home | 独立首页、适度标题、三张示例卡片、最近三项研究 |
| Research Composer | 多行主输入区、明确焦点边框、主 CTA、字数与 Ctrl/Cmd+Enter；参数留在设置 |
| Running | 展示原问题、真实状态、不定进度活动指示与次级取消；没有虚构百分比或完成阶段 |
| Result | 800px 阅读宽度、完整 Markdown、局部滚动表格、复制原始 Markdown、原问题预填追问、长标题限制与完整原问题折叠区 |
| Sources | 轻量来源卡片、域名/时间、外链、报告参考编号与证据绑定；按参考编号排列；正文引用跳转到对应卡片 |
| History | 最近 100 项任务的搜索列表，展示问题、时间、状态、来源数和部分结果；点击可打开、刷新也能恢复任务 |
| Knowledge | 独立资料列表、原生添加弹窗、搜索、删除；分块与原始位置默认折叠 |
| Settings | AI 服务、研究偏好、平台访问三组；生成参数、MCP 和评测信息默认折叠；超时/预算注明管理员配置 |

## 5. UX Simplification

开始研究只需输入问题并点击主按钮。示例一键预填，主页不再同时展示知识库和设置。
复制与延伸问题放在报告标题下，执行 Trace 与指标默认折叠。取消是次级操作，取消后停止活动动画。
知识库默认不显示分块数量，来源卡片不显示内部文档 ID。参数修改范围和离线演示模式明确说明。
过期任务响应不会覆盖新选择；运行期间不请求尚未发布的 Trace，避免重复 404。

## 6. Browser Validation

使用真实 Chromium/Playwright，在隔离运行目录启动应用。实际检查了旧版与新版，覆盖：

- 首页、示例预填、焦点/hover、研究创建、真实 completed、真实 running 与 DELETE cancelled。
- 通过仅存在于本地 QA 脚本的受控运行时故障，检查后端真实 failed 状态和错误详情。
- 报告、引用跳转、来源、复制反馈、追问预填、执行详情默认折叠与主动展开。
- History 搜索/无匹配/重新打开，Knowledge 添加/检索/删除/空状态，Settings 分区与高级折叠。
- 加载骨架通过延迟本地 API 响应检查；2000 字研究问题通过真实 API 完成，完整原问题保留。
- 1280、1440、1920、768、390px 窗口；长报告、表格、长问题无页面级横向溢出。

截图位于 `output/playwright/`（本地 QA 产物，已 gitignore）：
`before-home.png`、`after-home.png`、`after-report.png`、`after-sources.png`、
`after-history.png`、`after-knowledge.png`、`after-empty-knowledge.png`、`after-settings.png`、
`after-running.png`、`after-cancelled.png`、`after-failed.png`、`after-loading.png`、
`after-long-query.png`、`after-tablet.png`、`after-mobile.png`。

## 7. Full Validation

| Check | Result |
| --- | --- |
| Frontend logic/contract tests | 21 passed（原有 14 + 新增 7） |
| Python full suite | 400 passed, 1 skipped；跳过项为现有容器引擎相关拓扑测试 |
| Ruff lint / format | Passed |
| mypy | Passed, 72 source files |
| pip check | Passed |
| JS syntax / git diff whitespace | Passed |
| Build | sdist + wheel built with `python -m build --no-isolation`；已安装 wheel smoke 通过 CLI/API/MCP/离线研究 |
| Docker Compose | 配置解析和 `scripts/compose_smoke.py` 无引擎拓扑通过，包括 API→HTTP MCP→研究完成 |
| Docker / remote CI | 本机 Docker daemon 不可用，真实 image build/up 未执行；CI 工作流保持不变，本轮没有推送或触发远程 CI |

最初 Node 子进程测试受沙箱限制；使用允许子进程的执行环境后完整 pytest 通过。
没有把环境限制标成产品失败，也没有把未执行的 Docker/远程 CI 标成通过。

## 8. Remaining Visual Issues

没有发现阻碍内部试用的布局、对齐、截断或溢出问题。离线报告原文仍包含双语章节标题、
置信度和证据附录，内容呈现还有进一步精简空间；本轮保留原报告信息与复制内容。
浏览器验收以 Chromium 为主，尚未完成其他浏览器的视觉复核。

## 9. Internal User Testing

当前首页、报告、知识库与设置已经有统一的产品框架和清晰操作入口，可以直接截图给团队成员，
让他们在无操作指导的情况下尝试研究、阅读、查证和资料管理。此判断基于浏览器验收，尚未声称已开展真人用户测试。

**Ready for visual and usability user testing**
