---
id: kb-fixture-tools
title: 工具与权限
topic: tools
tags: [tools, mcp, permissions]
created_at: 2026-01-02T00:00:00Z
---
# 工具与权限

## 注册表

工具需要声明名称、描述、输入 Schema、输出 Schema、权限级别、超时时间与重试策略。

注册表负责路由、权限检查、超时控制、重试与 Trace 记录。

## MCP

MCP 基于 JSON-RPC 2.0，核心方法包括 initialize、tools/list 与 tools/call，传输层支持 stdio 与流式 HTTP。

## 权限

权限分为只读、网络、计算与写入四类，生产环境默认禁用写入权限并限制每任务调用次数。
