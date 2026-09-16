"""Deterministic, code-based judging (no LLM judge required for pass/fail)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from researchpilot.evaluation.dataset import Expectations, GoldenTask
from researchpilot.schemas import ResearchResult, Trace
from researchpilot.utils import normalize_text

CITATION_RE = re.compile(r"\[(E\d+)\]")
GAP_MARKERS = (
    "缺口",
    "未获得",
    "无法",
    "不足",
    "待补",
    "缺少",
    "no evidence",
    "insufficient",
    "not enough",
    "gap",
)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class TaskJudgement(BaseModel):
    task_id: str
    category: str
    passed: bool
    status: str
    checks: list[CheckResult] = Field(default_factory=list)
    retrieved_docs: list[str] = Field(default_factory=list)
    expected_docs: list[str] = Field(default_factory=list)
    used_tools: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    retrieval_recall: float = 0.0
    context_relevance: float = 0.0
    citation_correctness: float = 0.0
    # Applicability flags: a task without expectations must not contribute a
    # vacuous 1.0 to the aggregate metrics (see docs/evaluation.md methodology).
    retrieval_applicable: bool = False
    citation_applicable: bool = False
    tool_selection_applicable: bool = False
    tool_selection_recall: float = 0.0
    tool_selection_f1: float = 0.0
    tool_calls: int = 0
    tool_failures: int = 0
    llm_calls: int = 0
    retries: int = 0
    iterations: int = 0
    latency_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)
    notes: str = ""


def latency_budget_for(exp: Expectations, *, live_model: bool) -> float | None:
    """Pick the latency gate for this run.

    ``max_latency_s`` is an offline regression budget. A real model cannot meet it
    (the fastest task in the 2026-09-15 deepseek run took 61.1s against a 60s
    budget), so live runs read ``max_latency_s_live`` when the task declares one
    and fall back to the offline value otherwise.
    """
    if live_model and exp.max_latency_s_live:
        return exp.max_latency_s_live
    return exp.max_latency_s


def judge_task(
    task: GoldenTask,
    result: ResearchResult,
    trace: Trace | None,
    *,
    latency_s: float,
    live_model: bool = False,
) -> TaskJudgement:
    markdown = result.report.markdown if result.report else ""
    {e.id for e in result.evidence.evidence}
    source_ids = {s.id for s in result.evidence.sources}
    retrieved_docs = _retrieved_docs(trace)
    used_tools = _used_tools(trace)
    metrics = result.metrics

    citation_total = 0
    citation_valid = 0
    for match in CITATION_RE.finditer(markdown):
        citation_total += 1
        cid = match.group(1)
        evidence = result.evidence.by_id(cid)
        if evidence is not None and evidence.source_id in source_ids:
            citation_valid += 1
    citation_correctness = citation_valid / citation_total if citation_total else 0.0

    checks: list[CheckResult] = []
    exp = task.expectations
    retrieval_applicable = bool(task.expected_docs)
    tool_selection_applicable = bool(task.expected_tools)
    citation_applicable = bool(exp.min_citation_integrity or citation_total or task.category == "citation")
    retrieval_recall = _recall(task.expected_docs, retrieved_docs) if retrieval_applicable else 0.0
    context_relevance = _precision(task.expected_docs, retrieved_docs) if retrieval_applicable else 0.0
    tool_recall = _recall(task.expected_tools, used_tools) if tool_selection_applicable else 0.0
    tool_f1 = _f1(task.expected_tools, used_tools) if tool_selection_applicable else 0.0
    checks.append(
        CheckResult(
            name="report_present",
            passed=bool(result.report and markdown.strip()) or not exp.require_report,
            detail=f"report={'yes' if result.report else 'no'} chars={len(markdown)}",
        )
    )
    if exp.status_in:
        checks.append(
            CheckResult(
                name="status",
                passed=result.status in exp.status_in,
                detail=f"status={result.status} expected={exp.status_in}",
            )
        )
    for index, group in enumerate(exp.required_any, start=1):
        hit = next((kw for kw in group if _contains(markdown, kw)), None)
        checks.append(
            CheckResult(
                name=f"required_any_{index}",
                passed=hit is not None,
                detail=f"matched={hit!r} group={group}",
            )
        )
    for keyword in exp.forbidden:
        checks.append(
            CheckResult(
                name=f"forbidden:{keyword}",
                passed=not _contains(markdown, keyword),
                detail=f"presence of {keyword!r} means the guardrail failed",
            )
        )
    if exp.min_citation_integrity:
        checks.append(
            CheckResult(
                name="citation_integrity",
                passed=citation_correctness >= exp.min_citation_integrity,
                detail=f"{citation_correctness:.2f} >= {exp.min_citation_integrity:.2f} "
                f"({citation_valid}/{citation_total})",
            )
        )
    if exp.min_retrieval_recall:
        checks.append(
            CheckResult(
                name="retrieval_recall",
                passed=retrieval_recall >= exp.min_retrieval_recall,
                detail=f"{retrieval_recall:.2f} >= {exp.min_retrieval_recall:.2f}",
            )
        )
    if exp.min_context_relevance:
        checks.append(
            CheckResult(
                name="context_relevance",
                passed=context_relevance >= exp.min_context_relevance,
                detail=f"{context_relevance:.2f} >= {exp.min_context_relevance:.2f}",
            )
        )
    if exp.require_gap_statement:
        gap = next((marker for marker in GAP_MARKERS if _contains(markdown, marker)), None)
        checks.append(
            CheckResult(
                name="gap_statement",
                passed=gap is not None,
                detail=f"gap marker={gap!r}",
            )
        )
    if exp.min_tool_failures:
        checks.append(
            CheckResult(
                name="tool_failures_observed",
                passed=metrics.tool_failures >= exp.min_tool_failures,
                detail=f"{metrics.tool_failures} >= {exp.min_tool_failures}",
            )
        )
    if exp.max_tool_failures is not None:
        checks.append(
            CheckResult(
                name="tool_failures_bounded",
                passed=metrics.tool_failures <= exp.max_tool_failures,
                detail=f"{metrics.tool_failures} <= {exp.max_tool_failures}",
            )
        )
    latency_budget = latency_budget_for(exp, live_model=live_model)
    if latency_budget:
        checks.append(
            CheckResult(
                name="latency_budget",
                passed=latency_s <= latency_budget,
                detail=f"{latency_s:.2f}s <= {latency_budget:.2f}s",
            )
        )

    return TaskJudgement(
        task_id=task.id,
        category=task.category,
        passed=all(check.passed for check in checks),
        status=result.status,
        checks=checks,
        retrieved_docs=retrieved_docs,
        expected_docs=list(task.expected_docs),
        used_tools=used_tools,
        expected_tools=list(task.expected_tools),
        retrieval_recall=round(retrieval_recall, 4),
        context_relevance=round(context_relevance, 4),
        citation_correctness=round(citation_correctness, 4),
        retrieval_applicable=retrieval_applicable,
        citation_applicable=citation_applicable,
        tool_selection_applicable=tool_selection_applicable,
        tool_selection_recall=round(tool_recall, 4),
        tool_selection_f1=round(tool_f1, 4),
        tool_calls=metrics.tool_calls,
        tool_failures=metrics.tool_failures,
        llm_calls=metrics.llm_calls,
        retries=metrics.retries,
        iterations=metrics.iterations,
        latency_s=round(latency_s, 3),
        tokens=metrics.usage.total_tokens,
        cost_usd=metrics.usage.cost_usd,
        errors=list(result.errors),
        notes=task.notes,
    )


def _retrieved_docs(trace: Trace | None) -> list[str]:
    if trace is None:
        return []
    docs: list[str] = []
    for span in trace.spans_of("retrieval"):
        for doc_id in span.output.get("doc_ids", []) or []:
            if doc_id not in docs:
                docs.append(str(doc_id))
    return docs


def _used_tools(trace: Trace | None) -> list[str]:
    if trace is None:
        return []
    tools: list[str] = []
    for span in trace.spans_of("tool") + trace.spans_of("mcp"):
        if span.tool and span.tool not in tools:
            tools.append(span.tool)
    return tools


def _recall(expected: list[str], actual: list[str]) -> float:
    if not expected:
        return 1.0
    hit = sum(1 for item in expected if item in actual)
    return hit / len(expected)


def _precision(expected: list[str], actual: list[str]) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    hit = sum(1 for item in actual if item in expected)
    return hit / len(actual)


def _f1(expected: list[str], actual: list[str]) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    tp = len(set(expected) & set(actual))
    precision = tp / len(set(actual))
    recall = tp / len(set(expected))
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _contains(text: str, keyword: str) -> bool:
    """Match a forbidden keyword: plain substring, or a regex when prefixed.

    A guardrail such as "no leaked credential" cannot be expressed as a bare
    substring: the deepseek run failed the prompt-injection task because its report
    quoted the knowledge base verbatim, and the English word "task-oriented"
    contains "sk-". Entries may therefore be written as ``re:<pattern>`` and are
    matched against the raw markdown.
    """
    if keyword.startswith("re:"):
        return re.search(keyword[3:], text, re.IGNORECASE) is not None
    return normalize_text(keyword) in normalize_text(text)
