# ADR-0003: 离线优先的 Provider 与语料设计

- 状态：已采纳
- 日期：2026-09-13

## 背景

项目需要同时满足：CI 可稳定回归、演示不依赖网络、以及真实模型可用。

## 决策

1. `provider` 支持 `mock`（确定性脚本模型）与 `openai`（任意 OpenAI 兼容端点）；
2. 默认 `RESEARCHPILOT_PROVIDER=mock`、`EMBEDDING_PROVIDER=hash`、`WEB_SEARCH_MODE=offline`；
3. 内置 `data/web_corpus` 合成 Web 语料，元数据显式标记 `synthetic: true`；
4. 评测报告与 README 必须标注本次运行使用的 provider，并且不允许跨模式引用数字。

## 理由

离线确定性使得：CI 可以用"结果是否满足工程契约"作为回归门槛；评测过程可复现；
同时保留切换到真实模型/真实搜索的完整路径。

## 结果

优点：`pip install -e . && pytest` 在无网络环境即可完整跑通；离线评测衡量管线正确性，稳定、可对比。

代价：离线数字不能用来宣称模型质量。这一点通过 README 的 "Honesty Notes"、
`docs/evaluation.md` 的诚实声明、以及报告中的 provider 字段强制传达。
