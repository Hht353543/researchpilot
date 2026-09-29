# Windows 成品构建（维护者）

普通用户只拿 `ResearchPilot.exe`，不执行本文命令。Windows 10 / 11 x64 是桌面版本的目标平台；构建必须在 Windows x64 上进行。

## 可重复构建

已验证环境为 Windows 11、CPython 3.12.2 x64。核心依赖沿用 `constraints.txt`，桌面及打包工具版本记录在 `desktop-constraints.txt`。

在仓库根目录执行：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -c constraints.txt -c desktop-constraints.txt -e ".[dev,desktop-build]"
.\.venv-build\Scripts\python.exe -m pip check
.\.venv-build\Scripts\python.exe -m pytest tests/unit/test_desktop.py tests/integration/test_desktop_api.py -q
.\.venv-build\Scripts\python.exe scripts/build_windows.py
.\.venv-build\Scripts\python.exe scripts/desktop_smoke.py
```

产物是 `dist/ResearchPilot.exe`。PyInstaller 使用 `--onefile --windowed`，包含 Python、运行依赖、原生前端、jieba 分词资源、内置语料、配置默认资源和许可证。临时解压目录仅存放程序资源。数据在 `%LOCALAPPDATA%\ResearchPilot`，不写在 EXE 旁边。

正在运行的旧 EXE 不能被 Windows 覆盖。可通过 `python scripts/build_windows.py --dist-dir dist/new` 构建到新目录，避免中断现有实例。退出旧版后双击新版，数据目录仍保持原状。

桌面入口是 `scripts/desktop_entry.py` → `researchpilot.desktop.launcher.main`；原有 CLI、wheel、Docker 入口继续可用。桌面版本不读取 `.env`，也不继承其他项目的模型、网络和存储配置。模型连接使用前端配置及 Windows 当前用户 DPAPI。测试仅使用独立目录与假密钥，真实 Key 只能通过产品 UI 输入。

工作流 [windows-desktop.yml](../.github/workflows/windows-desktop.yml) 可手动触发，或通过 `desktop-v*` 标签运行。它会上传 EXE 和成品冒烟结果；发布状态以 GitHub Actions 的实际运行结果为准。

## 成品冒烟检查的边界

`scripts/desktop_smoke.py` 将成品复制到源码目录之外的临时中文、空格路径，隔离 PATH，制造 8000 端口冲突，检查资源、无 Key 启动、DPAPI、热更新、研究报告与引用、重复启动、原生托盘启动、退出及重启恢复。模型由本地兼容服务返回确定性响应。

结果写到 `output/desktop-smoke.json`。此检查验证真实 EXE 与 HTTP 链路，但**不能证明真实 DeepSeek 质量，也不等价于未安装开发工具的干净 Windows 环境**。

## 最终人工验收

1. 使用全新的 Windows 10 / 11 x64 普通权限用户，确认没有 Python、Git、Docker。
2. 把 EXE 放在源码目录之外，直接双击，确认浏览器自动打开首次设置。
3. 通过 UI 输入有效 DeepSeek Key；确认不需要任何其他配置即可完成第一次研究。
4. 阅读报告并定位引用，导入中文文件，确认资料范围标识准确。
5. 关闭浏览器，重新双击查看仍在运行的任务；再次双击不能产生第二个服务。
6. 在托盘或前端退出；重开确认设置、导入资料和历史保留。替换 EXE 后重复检查。
7. 模型服务失败、余额不足、限流时确认提示与操作可理解。

成品当前未签名，没有加入安装器、自动更新或额外服务。正式分发的 Windows 信任提示与干净环境兼容性仍需在上述环境实测。

[PyInstaller 单文件运行说明](https://pyinstaller.org/en/stable/operating-mode.html)、[Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)、[DeepSeek 接口](https://api-docs.deepseek.com/api/create-chat-completion/)、[OpenAI 模型预设](https://developers.openai.com/api/docs/models/gpt-4.1-mini)。
