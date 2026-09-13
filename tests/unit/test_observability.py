"""Trace system: nesting, metrics aggregation, cost estimation, persistence."""

from __future__ import annotations

from pathlib import Path

from researchpilot.observability.costs import estimate_cost
from researchpilot.observability.trace import Tracer, TraceStore
from researchpilot.schemas import TokenUsage


def test_spans_nest_and_aggregate() -> None:
    tracer = Tracer("task-1", "question")
    with tracer.span("PlannerAgent", "agent", agent="PlannerAgent") as outer:
        with tracer.span(
            "PlannerAgent.llm[planner]",
            "llm",
            agent="PlannerAgent",
            model="gpt-4o-mini",
        ) as inner:
            pass
        tracer.end_span(inner, usage=TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500))
    tracer.end_span(outer)

    assert len(tracer.spans) == 2
    assert tracer.spans[1].parent_id == tracer.spans[0].span_id
    metrics = tracer.metrics()
    assert metrics.llm_calls == 1
    assert metrics.usage.total_tokens == 1500
    assert metrics.usage.cost_usd > 0  # priced model
    assert metrics.agent_latency_ms["PlannerAgent"] >= 0
    assert metrics.latency_ms >= 0


def test_span_records_errors() -> None:
    tracer = Tracer("task-2")
    try:
        with tracer.span("boom", "tool", tool="calculator"):
            raise RuntimeError("kaboom")
    except RuntimeError:
        pass
    span = tracer.spans[0]
    assert span.error and "kaboom" in span.error


def test_metrics_count_tools_retries_and_failures() -> None:
    tracer = Tracer("task-3")
    for kind, name, error in [
        ("tool", "tool.web_search", None),
        ("tool", "tool.knowledge_search", "timeout"),
        ("retry", "retry.tool.knowledge_search", None),
        ("retrieval", "ResearchAgent.retrieval[hybrid]", None),
        ("mcp", "tool.mcp_research_context", None),
    ]:
        with tracer.span(name, kind) as span:  # type: ignore[arg-type]
            if error:
                tracer.end_span(span, error=error)
    metrics = tracer.metrics()
    assert metrics.tool_calls == 2
    assert metrics.tool_failures == 1
    assert metrics.retries == 1
    assert metrics.retrieval_calls == 1
    assert metrics.mcp_calls == 1


def test_trace_store_persists_and_reloads(tmp_path: Path) -> None:
    tracer = Tracer("task-store", "q")
    with tracer.span("PlannerAgent", "agent", agent="PlannerAgent"):
        pass
    trace = tracer.finish()
    store = TraceStore(tmp_path / "runs")
    store.save(trace)
    assert (tmp_path / "runs" / "task-store.trace.json").exists()
    fresh = TraceStore(tmp_path / "runs")
    loaded = fresh.get("task-store")
    assert loaded is not None
    assert loaded.task_id == "task-store"
    assert loaded.spans[0].name == "PlannerAgent"
    assert fresh.get("missing") is None
    assert "task-store" in fresh.all_task_ids()


def test_cost_estimation_uses_price_table() -> None:
    table = {"gpt-4o-mini": {"input": 0.15, "output": 0.6}, "default": {"input": 0.0, "output": 0.0}}
    cost = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000, table)
    assert abs(cost - 0.75) < 1e-9
    assert estimate_cost("unknown-model", 1000, 1000, table) == 0.0
