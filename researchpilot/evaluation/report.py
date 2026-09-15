"""Markdown generators: docs/evaluation.md and resume bullets from real numbers."""

from __future__ import annotations

from pathlib import Path

from researchpilot.evaluation.runner import EvaluationReport


def _rate(value: float | None, *, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}%}"


def _fmt(value: float | None, *, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _is_mock_run(report: EvaluationReport) -> bool:
    """True when the numbers came from the offline deterministic provider."""
    return report.provider.strip().lower() == "mock"


def _provider_note(report: EvaluationReport) -> str:
    """One line saying where the numbers came from, for the eval report."""
    if _is_mock_run(report):
        return (
            f"- 本次运行使用 `{report.provider}` provider（{report.mode}）。Mock provider 是确定性脚本模型，"
            "用来验证工程管线、回归与 CI；它的数字衡量检索、工具、校验、追踪、评测这些环节，"
            "不代表前沿模型的生成质量。"
        )
    return (
        f"- 本次运行使用 `{report.provider}` provider（{report.mode}），数字来自真实模型调用"
        f"（{report.model}），不是 mock 基线的复述。"
    )


def render_evaluation_markdown(report: EvaluationReport) -> str:
    metrics = report.metrics
    lines: list[str] = [
        "# Evaluation Report",
        "",
        "本文件由 `scripts/run_benchmark.py` 在真实运行后自动生成，里面的数字来自本次运行。",
        "",
        "## 运行信息 (Run Metadata)",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| run_id | `{report.run_id}` |",
        f"| 生成时间 (UTC) | {report.generated_at} |",
        f"| LLM provider | `{report.provider}` |",
        f"| 模型 | `{report.model}` |",
        f"| 运行模式 | {report.mode} |",
        f"| 数据集 | `{report.dataset_path}` |",
        f"| 总耗时 | {report.duration_s:.1f}s |",
        f"| Python / Platform | {report.environment.get('python')} / {report.environment.get('platform')} |",
        f"| Embedding | {report.environment.get('embedding_provider')} "
        f"(dim={report.environment.get('embedding_dim')}) |",
        f"| 检索 top_k | {report.environment.get('retrieval_top_k')} |",
        f"| MCP transport | {report.environment.get('mcp_transport')} |",
        f"| Web search mode | {report.environment.get('web_search_mode')} |",
        "",
        "## 数据集 (Golden Dataset)",
        "",
        f"- 任务数：{report.dataset.get('tasks', 0)}",
        f"- 含期望来源文档的任务：{report.dataset.get('with_expected_docs', 0)}",
        f"- 含期望工具的任务：{report.dataset.get('with_expected_tools', 0)}",
        f"- 含故障注入的任务：{report.dataset.get('fault_injection_tasks', 0)}",
        "",
        "| 类别 | 任务数 |",
        "| --- | --- |",
    ]
    for category, count in (report.dataset.get("categories") or {}).items():
        lines.append(f"| {category} | {count} |")

    lines += [
        "",
        "## 总体指标 (Overall Metrics)",
        "",
        "| 指标 | 值 | 说明 |",
        "| --- | --- | --- |",
        f"| Task Success Rate | {metrics.task_success_rate:.1%} "
        f"({metrics.passed}/{metrics.tasks}) | 通过全部硬性判定的任务比例 |",
        f"| Retrieval Recall@{report.environment.get('retrieval_top_k')} | "
        f"{metrics.retrieval_recall:.1%} | 期望来源文档出现在检索结果中的比例"
        f"（分母 {metrics.retrieval_tasks} 个含期望来源的任务） |",
        f"| Context Relevance | {metrics.context_relevance:.1%} | "
        f"检索结果中相关文档占比 (precision@k)，同一分母 |",
        f"| Citation Correctness | {metrics.citation_correctness:.1%} | "
        f"引用可解析且能支撑结论的比例（分母 {metrics.citation_tasks} 个要求引用的任务） |",
        f"| Tool Selection Accuracy | {metrics.tool_selection_accuracy:.1%} | "
        f"期望工具被调用的比例（分母 {metrics.tool_selection_tasks} 个含期望工具的任务） |",
        f"| Tool Selection F1 | {metrics.tool_selection_f1:.1%} | 工具集合的 F1，同一分母 |",
        f"| Tool Success Rate | {_rate(metrics.tool_success_rate)} | "
        f"1 - 失败调用 / 总调用（{metrics.tool_calls} 次调用，含故障注入） |",
        f"| Avg Latency | {metrics.avg_latency_s:.2f}s | 端到端平均耗时 |",
        f"| P50 / P95 Latency | {metrics.p50_latency_s:.2f}s / {metrics.p95_latency_s:.2f}s | 延迟分布 |",
        f"| Token Usage (total) | {metrics.total_tokens:,} | 输入 + 输出 token (估算/上报) |",
        f"| Avg Tokens / Task | {metrics.avg_tokens:,.1f} | 单任务平均 token |",
        f"| Cost (total) | ${metrics.total_cost_usd:.6f} | 按 `configs/pricing.yaml` 计价 |",
        f"| Avg Cost / Task | ${metrics.avg_cost_usd:.6f} | "
        f"{'离线 mock provider 成本为 0' if _is_mock_run(report) else '按 `configs/pricing.yaml` 计价'} |",
        f"| Tool Calls / Failures | {metrics.tool_calls} / {metrics.tool_failures} | 含故障注入任务 |",
        f"| LLM Calls / Retries | {metrics.llm_calls} / {metrics.retries} | 重试来自 Schema 修复与工具重试 |",
        f"| Avg Iterations | {metrics.avg_iterations:.2f} | 研究迭代轮数 |",
        f"| Writer Fallback Reports | {metrics.writer_fallback_tasks} / {metrics.tasks} | "
        f"模型报告没有引用绑定，改由确定性抽取器产出；这些任务的引用指标不代表模型写作能力 |",
        "",
        "## 分类指标 (Per-category)",
        "",
        "| 类别 | 任务 | 通过 | 成功率 | 平均延迟 | 平均 token | 引用正确率 | 召回率 | 工具 F1 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.categories:
        lines.append(
            f"| {row.category} | {row.tasks} | {row.passed} | {row.task_success_rate:.1%} | "
            f"{row.avg_latency_s:.2f}s | {row.avg_tokens:,.0f} | "
            f"{_rate(row.citation_correctness)} | {_rate(row.retrieval_recall)} | "
            f"{_rate(row.tool_selection_f1)} |"
        )

    lines += [
        "",
        "> 说明：Recall / Context Relevance 仅在「声明了期望来源文档」的任务上取平均"
        f"（{metrics.retrieval_tasks}/{metrics.tasks} 个任务）；"
        f"Citation Correctness 仅在要求引用的任务上取平均（{metrics.citation_tasks}/{metrics.tasks}）；"
        f"Tool Selection 仅在声明了期望工具的任务上取平均（{metrics.tool_selection_tasks}/{metrics.tasks}）。"
        "没有期望值的任务不再贡献「真空 1.0」，也不会稀释或抬高这些指标。",
    ]

    if report.failed_checks:
        lines += ["", "## 未通过的检查项 (Failed Checks)", "", "| 检查项 | 失败任务数 |", "| --- | --- |"]
        for name, count in report.failed_checks.items():
            lines.append(f"| {name} | {count} |")

    lines += [
        "",
        "## 逐任务结果 (Per-task)",
        "",
        "| 任务 | 类别 | 通过 | 状态 | 延迟 | token | 工具调用/失败 | 引用正确率 | 失败检查 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for judgement in report.judgements:
        failed = ", ".join(check.name for check in judgement.checks if not check.passed) or "-"
        lines.append(
            f"| `{judgement.task_id}` | {judgement.category} | "
            f"{'通过' if judgement.passed else '失败'} | {judgement.status} | "
            f"{judgement.latency_s:.2f}s | {judgement.tokens:,} | "
            f"{judgement.tool_calls}/{judgement.tool_failures} | "
            f"{judgement.citation_correctness:.1%} | {failed} |"
        )

    lines += [
        "",
        "## 指标定义 (Metric Definitions)",
        "",
        "- Task Success Rate：任务通过全部硬性判定（报告存在、关键词覆盖、引用可解析、"
        "无禁用内容、缺口显式声明、故障处理符合预期等）才计为成功。",
        "- Retrieval Recall@k：期望来源文档出现在 top-k 检索结果中的比例（数据集提供弱标注），"
        "仅在含期望来源的任务上取平均。",
        "- Context Relevance：top-k 检索结果中属于期望文档集合的比例。",
        "- Citation Correctness：Markdown 中 `[E#]` 引用能解析到已登记证据、且该证据映射到真实来源的比例"
        "（分母为要求引用的任务数）。",
        "- Tool Selection Accuracy / F1：实际调用工具集合与数据集期望工具集合的召回与 F1"
        "（分母为声明期望工具的任务数）。",
        "- Tool Success Rate：1 - 工具失败次数 / 工具调用总次数（故障注入任务会主动制造失败）；"
        "没有任何工具调用时记为 `n/a` 而不是 1.0。",
        "- Latency / Token / Cost：来自统一 Trace 的端到端测量与真实 token 计数/估算，"
        "成本按配置文件价格表计算。",
        "",
        "## 复现方式 (Reproduce)",
        "",
        "```bash",
        "# 离线确定性模式（无需 API Key，CI 使用）",
        "python scripts/run_benchmark.py --provider mock",
        "",
        "# 真实模型模式（需要 API Key）",
        "export API_KEY=sk-...        # 或 RESEARCHPILOT_API_KEY",
        "export MODEL=gpt-4o-mini",
        "export BASE_URL=https://api.openai.com/v1",
        "python scripts/run_benchmark.py --provider openai",
        "```",
        "",
        "## 数据说明",
        "",
        _provider_note(report),
        "- `web_search` 在离线模式下检索 `data/web_corpus` 里的合成示例语料"
        "（元数据标记 `synthetic: true`），让系统在没有网络时也能跑通；它不是真实搜索结果。",
        "- 用真实模型（`--provider openai`）重新运行本脚本会覆盖本文件。",
    ]
    return "\n".join(lines).strip() + "\n"


def render_resume_section(report: EvaluationReport) -> str:
    metrics = report.metrics
    mode = "离线确定性模式（mock provider）" if report.provider == "mock" else f"{report.model}"
    resumable_note = (
        "- 上表来自离线确定性 provider（mock），衡量的是检索、工具、校验、引用绑定、追踪、评测"
        "这些工程环节，不代表真实模型的生成质量。"
        if _is_mock_run(report)
        else f"- 上表来自真实模型调用（`{report.provider}`，{mode}），数字是本次运行的实测值；"
        "离线 mock 基线见 `docs/evaluation.md`。"
    )
    if metrics.writer_fallback_tasks:
        resumable_note += (
            f"\n- 其中 {metrics.writer_fallback_tasks} / {metrics.tasks} 条报告由确定性回退写作器产出"
            "（模型报告没有引用绑定），引用类数字不能算作模型的写作能力。"
        )
    return (
        "\n".join(
            [
                "# 简历项目描述（依据本次真实评测数据生成）",
                "",
                f"> 数据来源：{report.generated_at} 的评测运行，provider=`{report.provider}`（{mode}），"
                f"数据集 {metrics.tasks} 个任务，耗时 {report.duration_s:.0f}s。"
                "若使用真实模型重新运行 benchmark，请用新数据替换下列数字。",
                "",
                "## 项目名称",
                "",
                "ResearchPilot —— 多 Agent 深度研究与知识库平台",
                "",
                "## 项目描述（简历 bullet）",
                "",
                f"- 设计并实现多 Agent 深度研究系统（Planner/Researcher/Verifier/Critic/Writer），"
                f"以 Pydantic 结构化 Schema 承载全部 Agent 状态，"
                f"通过 JSON Schema 约束 + 校验失败自动修复重试，"
                f"在 {metrics.tasks} 条 Golden Dataset 上取得 {metrics.task_success_rate:.1%} 任务成功率"
                f"（引用可解析率 {metrics.citation_correctness:.1%}）。",
                f"- 构建可评测 RAG 管线（Loader→Cleaner→Heading-aware Chunker→Embedding→Vector Store→"
                f"Hybrid Retrieval→Reranker），实现语义/关键词/RRF 混合检索、元数据过滤、查询改写与重排，"
                f"检索 Recall@{report.environment.get('retrieval_top_k')} 达 {metrics.retrieval_recall:.1%}、"
                f"上下文相关性 {metrics.context_relevance:.1%}。",
                f"- 实现统一 Tool Registry（Schema 校验 + 权限分级 + 超时 + 重试 + Trace）与独立 MCP Server"
                f"（JSON-RPC 2.0，stdio/HTTP 双传输，暴露 search_knowledge/get_document/search_web/"
                f"get_research_context），工具选择召回 {metrics.tool_selection_accuracy:.1%}、"
                f"工具成功率 {_rate(metrics.tool_success_rate)}，"
                f"并用故障注入（超时/失败/空结果/调用预算）验证韧性。",
                f"- 建立真实测量的评测与可观测体系：Golden Dataset 覆盖 12 类场景（含 Prompt Injection、"
                f"工具滥用、超时恢复、无结果、低质量来源），"
                f"统一 Trace 记录 Task→Agent→LLM/Tool/Retrieval/Retry "
                f"的延迟与 token；端到端平均延迟 {metrics.avg_latency_s:.2f}s"
                f"（P95 {metrics.p95_latency_s:.2f}s），"
                f"单任务平均 {metrics.avg_tokens:,.0f} token、"
                f"成本 ${metrics.avg_cost_usd:.6f}（按价格表计价）。",
                "",
                "## 使用说明",
                "",
                resumable_note,
                "- 数字可以对着 `docs/evaluation.md` 和 `benchmarks/` 下的原始 JSON 逐条核对。",
                "- 用真实模型（`--provider openai`）重跑会生成新数据，本文件会被覆盖。",
            ]
        ).strip()
        + "\n"
    )


def write_docs(
    report: EvaluationReport,
    *,
    docs_dir: str | Path = Path("docs"),
    resume: bool = True,
    filename: str = "evaluation.md",
) -> list[Path]:
    directory = Path(docs_dir)
    directory.mkdir(parents=True, exist_ok=True)
    evaluation = directory / filename
    evaluation.write_text(render_evaluation_markdown(report), encoding="utf-8")
    written = [evaluation]
    if resume:
        resume_path = directory / "resume.md"
        resume_path.write_text(render_resume_section(report), encoding="utf-8")
        written.append(resume_path)
    return written
