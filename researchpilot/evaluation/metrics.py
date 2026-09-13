"""Aggregate metrics computed from real per-task runs."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from researchpilot.evaluation.judge import TaskJudgement


class EvaluationMetrics(BaseModel):
    tasks: int = 0
    passed: int = 0
    task_success_rate: float = 0.0
    retrieval_tasks: int = 0
    citation_tasks: int = 0
    tool_selection_tasks: int = 0
    retrieval_recall: float = 0.0
    context_relevance: float = 0.0
    citation_correctness: float = 0.0
    tool_selection_accuracy: float = 0.0
    tool_selection_f1: float = 0.0
    tool_success_rate: float | None = None
    tool_calls: int = 0
    tool_failures: int = 0
    retries: int = 0
    llm_calls: int = 0
    avg_latency_s: float = 0.0
    p50_latency_s: float = 0.0
    p95_latency_s: float = 0.0
    total_tokens: int = 0
    avg_tokens: float = 0.0
    total_cost_usd: float = 0.0
    avg_cost_usd: float = 0.0
    avg_iterations: float = 0.0


class CategoryMetrics(BaseModel):
    category: str
    tasks: int = 0
    passed: int = 0
    task_success_rate: float = 0.0
    avg_latency_s: float = 0.0
    avg_tokens: float = 0.0
    retrieval_tasks: int = 0
    tool_selection_tasks: int = 0
    # ``None`` = "no task in this category declares that expectation"; the report
    # renders it as n/a instead of a misleading 0.0%.
    citation_correctness: float | None = None
    retrieval_recall: float | None = None
    context_relevance: float | None = None
    tool_selection_f1: float | None = None


def aggregate(judgements: list[TaskJudgement]) -> EvaluationMetrics:
    if not judgements:
        return EvaluationMetrics()
    latencies = sorted(j.latency_s for j in judgements)
    tool_calls = sum(j.tool_calls for j in judgements)
    tool_failures = sum(j.tool_failures for j in judgements)
    total_tokens = sum(j.tokens for j in judgements)
    total_cost = sum(j.cost_usd for j in judgements)
    count = len(judgements)
    # Only tasks that actually declare an expectation contribute to the metric:
    # averaging a "vacuous 1.0" over tasks without expectations would inflate
    # Recall / Citation / Tool-selection numbers.
    retrieval_tasks = [j for j in judgements if j.retrieval_applicable]
    citation_tasks = [j for j in judgements if j.citation_applicable]
    tool_tasks = [j for j in judgements if j.tool_selection_applicable]
    return EvaluationMetrics(
        tasks=count,
        passed=sum(1 for j in judgements if j.passed),
        task_success_rate=round(sum(1 for j in judgements if j.passed) / count, 4),
        retrieval_tasks=len(retrieval_tasks),
        citation_tasks=len(citation_tasks),
        tool_selection_tasks=len(tool_tasks),
        retrieval_recall=round(_mean(j.retrieval_recall for j in retrieval_tasks), 4),
        context_relevance=round(_mean(j.context_relevance for j in retrieval_tasks), 4),
        citation_correctness=round(_mean(j.citation_correctness for j in citation_tasks), 4),
        tool_selection_accuracy=round(_mean(j.tool_selection_recall for j in tool_tasks), 4),
        tool_selection_f1=round(_mean(j.tool_selection_f1 for j in tool_tasks), 4),
        tool_success_rate=(round(1 - tool_failures / tool_calls, 4) if tool_calls else None),
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        retries=sum(j.retries for j in judgements),
        llm_calls=sum(j.llm_calls for j in judgements),
        avg_latency_s=round(_mean(latencies), 3),
        p50_latency_s=round(_percentile(latencies, 50), 3),
        p95_latency_s=round(_percentile(latencies, 95), 3),
        total_tokens=total_tokens,
        avg_tokens=round(total_tokens / count, 1),
        total_cost_usd=round(total_cost, 6),
        avg_cost_usd=round(total_cost / count, 6),
        avg_iterations=round(_mean(j.iterations for j in judgements), 3),
    )


def by_category(judgements: list[TaskJudgement]) -> list[CategoryMetrics]:
    grouped: dict[str, list[TaskJudgement]] = defaultdict(list)
    for judgement in judgements:
        grouped[judgement.category].append(judgement)
    rows: list[CategoryMetrics] = []
    for category, items in sorted(grouped.items()):
        retrieval_items = [j for j in items if j.retrieval_applicable]
        tool_items = [j for j in items if j.tool_selection_applicable]
        citation_items = [j for j in items if j.citation_applicable]
        rows.append(
            CategoryMetrics(
                category=category,
                tasks=len(items),
                passed=sum(1 for j in items if j.passed),
                task_success_rate=round(sum(1 for j in items if j.passed) / len(items), 4),
                avg_latency_s=round(_mean(j.latency_s for j in items), 3),
                avg_tokens=round(_mean(j.tokens for j in items), 1),
                retrieval_tasks=len(retrieval_items),
                tool_selection_tasks=len(tool_items),
                citation_correctness=_mean_or_none(j.citation_correctness for j in citation_items),
                retrieval_recall=_mean_or_none(j.retrieval_recall for j in retrieval_items),
                context_relevance=_mean_or_none(j.context_relevance for j in retrieval_items),
                tool_selection_f1=_mean_or_none(j.tool_selection_f1 for j in tool_items),
            )
        )
    return rows


def failed_checks(judgements: list[TaskJudgement]) -> dict[str, int]:
    failures: dict[str, int] = defaultdict(int)
    for judgement in judgements:
        for check in judgement.checks:
            if not check.passed:
                failures[check.name.split(":")[0]] += 1
    return dict(sorted(failures.items(), key=lambda item: item[1], reverse=True))


def _mean(values: Any) -> float:
    items = list(values)
    if not items:
        return 0.0
    return sum(float(v) for v in items) / len(items)


def _mean_or_none(values: Any) -> float | None:
    items = list(values)
    if not items:
        return None
    return round(sum(float(v) for v in items) / len(items), 4)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (percentile / 100) * (len(sorted_values) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight
