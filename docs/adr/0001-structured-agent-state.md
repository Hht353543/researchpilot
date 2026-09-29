# ADR-0001: Agent 状态使用 Pydantic 结构化 Schema

- 状态：已采纳
- 日期：2026-09-13

## 背景

多 Agent 系统最常见的技术债是"用字符串传递状态"：Agent 之间传递自由文本，下游用正则或
关键字解析。这导致无法校验、无法重试、无法评测，任何 Prompt 微调都会引发级联故障。

## 决策

所有跨 Agent 的状态都定义为 Pydantic 模型（`ResearchPlan` / `EvidenceBundle` /
`VerificationReport` / `CritiqueReport` / `FinalReport`），并且：

1. LLM 调用通过 `StructuredLLMRunner` 强制 JSON Schema 校验；
2. 校验失败时携带错误信息进行有限次数的修复重试；
3. 校验持续失败时降级为确定性回退实现（而不是抛出异常中断任务）；
4. 关键字段在代码层再做一次业务校验（工具是否可用、引用是否存在、子任务是否有覆盖）。

## 结果

优点：可测试（每个 Schema 都有单测）、可演进（字段版本化）、可评测（判分基于字段而非文本）、
失败可定位（错误信息带字段路径）。

代价：Prompt 需要维护 JSON 结构说明；对模型的指令遵循能力有要求（因此保留修复重试与回退路径）。
