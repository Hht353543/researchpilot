# ResearchPilot 项目描述（2026-10-06 更新）

定位：AI 应用开发 / Python 后端方向。以下是基于交付证据的项目候选表述，个人职责需在投递前按实际参与范围确认；项目为 AI 辅助开发，不把代理执行的验收写成独立用户测试。

## 简历项目要点

**ResearchPilot —— Windows 个人资料研究助手**

- 开发支持 PDF、DOCX、Markdown 等资料导入的 Windows EXE，将资料选择、研究、继承追问、原文引用、HTML/Markdown 导出及研究记录恢复串成完整流程；本机成品操作录像与验收记录已保留。
- 基于 Planner、Researcher、Verifier、Critic、Writer 构建研究流程，结合结构化输出、资料范围隔离和引用绑定；修复摘要漏答日期/年份金额、计算引用挤占原始答案及同文档重复摘录，保留否定、单位和必要条件。
- 建立六类合成资料的真实模型回归，覆盖否定、数字、日期、条件、冲突及无答案，并追加两次追问；固定 deepseek-flash 及实验参数，记录 61 次调用、142,933 tokens、平均19.950秒，并完成 HTTP usage、任务指标与 Trace 用量对账。
- 完成 Python 回归489通过、1跳过和前端34/34；在源码外 EXE 上验证受限 PATH、端口回退、DPAPI、单实例与重启恢复，将工程检查、真实答案核对及环境验收分别记录。

## 可核验边界

八题核心答案经 Codex 对原文核对无遗漏；两题辅助文字仍有无依据限定或错误的缺失判断，不能写整篇正确率100%。本机功能流程通过，干净 Windows、独立用户验收和人工账单核对仍待完成。单次短资料实验不代表真实用户覆盖、性能保证或通用成本下降。

【待确认】个人具体负责的设计、实现和决策范围。确认后将第一、二条动词调整到真实职责；不使用未经确认的“主导”“负责人”或“独立完成”。

证据：[交付与逐题验收](goal_delivery_20261006.md)、[主张账本](resume_project_evidence.json)、[演示视频](../output/playwright/goal-20261006/researchpilot-final-demo.webm)。历史35题真实模型成绩见 [resume_live_deepseek.md](resume_live_deepseek.md)，历史 mock 指标见 evaluation.md；本轮没有把历史指标合并到六类资料成绩中。
