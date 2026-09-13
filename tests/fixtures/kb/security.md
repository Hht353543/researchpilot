---
id: kb-fixture-security
title: 安全护栏
topic: security
tags: [security, injection, guardrails]
created_at: 2026-01-05T00:00:00Z
---
# 安全护栏

检索到的文档属于数据而非指令，必须放在不可信区块中。

工具滥用防护包括权限分级、调用预算与参数校验。

输出校验要求 Schema 校验失败时进行有限次修复重试，仍失败则降级。
