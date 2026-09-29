---
id: kb-agent-architecture
title: Agent 架构模式与编排方式
source: internal://handbook/agent-architecture
topic: architecture
tags: [agent, architecture, orchestration, reliability]
created_at: 2026-02-11T08:00:00Z
---
# Agent 架构模式与编排方式

## 三种主流控制流

ReAct 模式把推理与行动交错进行：模型先给出思考，再选择工具，观察结果后继续推理。它实现简单、token 开销低，适合步骤少、反馈快的任务，但在长任务中容易丢失全局目标。

Plan-and-Execute 模式先由 Planner 产出结构化计划，再由 Executor 逐步执行。它把"想做什么"和"怎么做"解耦，便于对计划做校验、并行化和中断恢复，代价是需要维护计划状态并在执行偏差时重新规划。

Reflection（含 Critic/Verifier 角色）模式在产出前后增加自我审查环节：验证证据是否支撑结论、发现覆盖缺口、决定是否补检。它显著提升事实性任务的可信度，但会引入额外的模型调用与延迟。

## 多 Agent 协作的两种拓扑

中心化（Orchestrator-Worker）拓扑由一个编排者持有全局状态，工人 Agent 只负责局部子任务，便于控制预算、注入权限和重放排错，是生产系统更常见的选择。

去中心化（Peer-to-Peer）拓扑让 Agent 之间直接对话，灵活但难以控制成本、难以确定收敛条件，且调试复杂，通常需要额外的仲裁者。

## 结构化状态优先

工程上应避免用自然语言字符串承载 Agent 状态。计划、子任务、证据、校验结论都应有显式 Schema（例如 Pydantic 模型），这样才能做参数校验、版本演进、重试恢复和自动评测。

## 常见失败模式

1. 无限循环：Critic 持续要求补检。必须设置 max_iterations、时间预算和 token 预算。
2. 上下文溢出：证据不断累积导致超出模型窗口。需要摘要、排序和截断策略。
3. 工具误用：Agent 选择不合适的工具或参数。需要工具描述、参数校验和选择准确率指标。
4. 结论漂移：写作阶段引入未被证据支持的结论。需要在写作者之后做引用与证据绑定校验。
5. 静默降级：外部依赖失败后系统仍输出结论。必须显式记录 errors 与 degraded 状态。
