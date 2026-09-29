# ADR-0002: 自研 MCP Server（JSON-RPC 2.0）而非引入 SDK

- 状态：已采纳
- 日期：2026-09-13

## 背景

项目要求提供独立 MCP Server，暴露 `search_knowledge` / `get_document` / `search_web` /
`get_research_context`。可选方案：引入官方/社区 MCP SDK，或按协议自行实现最小可用子集。

## 决策

自行实现 `initialize` / `tools/list` / `tools/call` 与 stdio、streamable-HTTP 两种传输，
协议层与传输层分离（`McpServer` 与 `serve_stdio` / `create_http_app` 解耦）。

## 理由

1. 运行环境无网络，引入新依赖不可行；离线可运行是本项目的硬约束；
2. 协议面小（JSON-RPC 2.0 + 4 个工具），自研成本可控，且能精确控制超时、错误码与结构化输出；
3. 自研实现便于在教学/面试场景中把协议细节讲清楚；
4. 传输层可插拔（in-process 用于单进程与测试，stdio 对接桌面客户端，HTTP 用于容器化独立部署）。

## 结果

优点：零额外依赖、协议行为可测（三种传输各有集成测试）、部署形态灵活。

代价：需要自行跟进 MCP 协议演进（如 resources/prompts 原语、认证扩展）。
若后续需要完整协议兼容，可把 `McpServer.handle` 适配到官方 SDK，客户端与服务端接口保持不变。
