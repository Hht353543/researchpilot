---
id: kb-fixture-agents
title: Agent 编排基础
topic: architecture
tags: [agent, orchestration]
created_at: 2026-01-01T00:00:00Z
---
# Agent 编排基础

## 控制流

ReAct 模式交错进行推理与工具调用，实现简单但长任务容易丢失全局目标。

Plan-and-Execute 模式先产出结构化计划再逐步执行，便于校验与中断恢复。

## 拓扑

中心化拓扑由编排者持有全局状态，便于控制预算与权限。

去中心化拓扑让 Agent 直接对话，灵活但收敛条件难以确定。
