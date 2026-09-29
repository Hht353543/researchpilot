"""Unified trace system: Task -> Agent -> (LLM | Tool | Retrieval | Retry)."""

from __future__ import annotations

import calendar
import contextlib
import json
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from researchpilot.config import get_settings
from researchpilot.observability.costs import estimate_cost
from researchpilot.persistence import PersistenceCorruptionError, PersistenceError, atomic_write_text
from researchpilot.schemas import Span, SpanKind, TaskMetrics, TaskStatus, TokenUsage, Trace
from researchpilot.utils import new_id, utc_now_iso

_current_tracer: ContextVar[Tracer | None] = ContextVar("researchpilot_tracer", default=None)


def current_tracer() -> Tracer | None:
    return _current_tracer.get()


@contextlib.contextmanager
def tracer_scope(tracer: Tracer) -> Iterator[Tracer]:
    token = _current_tracer.set(tracer)
    try:
        yield tracer
    finally:
        _current_tracer.reset(token)


class Tracer:
    """Records spans for one research task.

    Spans nest automatically: the span of the ambient context becomes the
    parent, which produces the ``Task -> Agent -> Tool`` tree used by the UI.
    """

    def __init__(
        self,
        task_id: str,
        question: str = "",
        *,
        trace_id: str | None = None,
        price_table: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.trace_id = trace_id or new_id("trace")
        self.task_id = task_id
        self.question = question
        self.started_at = utc_now_iso()
        self.ended_at: str = ""
        self.spans: list[Span] = []
        self._stack: list[str] = []
        self._lock = threading.Lock()
        self._price_table = price_table if price_table is not None else get_settings().price_table()

    # -- span creation ----------------------------------------------------- #
    def start_span(
        self,
        name: str,
        kind: SpanKind,
        *,
        agent: str = "",
        tool: str = "",
        model: str = "",
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        with self._lock:
            parent = self._stack[-1] if self._stack else None
            span = Span(
                span_id=new_id("span"),
                trace_id=self.trace_id,
                parent_id=parent,
                name=name,
                kind=kind,
                agent=agent,
                tool=tool,
                model=model,
                start_time=utc_now_iso(),
                input=input or {},
                metadata=metadata or {},
            )
            self._stack.append(span.span_id)
            self.spans.append(span)
        return span

    def end_span(
        self,
        span: Span,
        *,
        output: dict[str, Any] | None = None,
        usage: TokenUsage | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Span:
        with self._lock:
            span.end_time = utc_now_iso()
            span.latency_ms = round(max(span.latency_ms, _elapsed_ms(span.start_time)), 3)
            if output is not None:
                span.output = output
            if error is not None:
                span.error = error
            if metadata:
                span.metadata.update(metadata)
            if usage is not None:
                span.usage = usage
                if usage.cost_usd == 0.0 and span.model:
                    span.usage.cost_usd = estimate_cost(
                        span.model, usage.prompt_tokens, usage.completion_tokens, self._price_table
                    )
            if self._stack and self._stack[-1] == span.span_id:
                self._stack.pop()
            elif span.span_id in self._stack:
                self._stack.remove(span.span_id)
        return span

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        kind: SpanKind,
        *,
        agent: str = "",
        tool: str = "",
        model: str = "",
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        span = self.start_span(
            name, kind, agent=agent, tool=tool, model=model, input=input, metadata=metadata
        )
        try:
            yield span
        except Exception as exc:
            self.end_span(span, error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            if not span.end_time:
                self.end_span(span)

    # -- aggregation -------------------------------------------------------- #
    def finish(self) -> Trace:
        self.ended_at = utc_now_iso()
        return self.to_trace()

    def metrics(self) -> TaskMetrics:
        usage = TokenUsage()
        agent_latency: dict[str, float] = {}
        metrics = TaskMetrics()
        for span in self.spans:
            usage = usage.add(span.usage)
            if span.kind == "llm":
                metrics.llm_calls += 1
            elif span.kind == "tool":
                metrics.tool_calls += 1
                if span.error:
                    metrics.tool_failures += 1
            elif span.kind == "retrieval":
                metrics.retrieval_calls += 1
            elif span.kind == "retry":
                metrics.retries += 1
            elif span.kind == "mcp":
                metrics.mcp_calls += 1
            if span.kind == "agent":
                agent_latency[span.name] = round(agent_latency.get(span.name, 0.0) + span.latency_ms, 3)
        metrics.usage = usage
        metrics.agent_latency_ms = agent_latency
        if self.spans:
            metrics.latency_ms = round(sum(s.latency_ms for s in self.spans if s.parent_id is None), 3)
        return metrics

    def to_trace(self, *, status: TaskStatus = "completed") -> Trace:
        return Trace(
            trace_id=self.trace_id,
            task_id=self.task_id,
            question=self.question,
            status=status,
            spans=list(self.spans),
            started_at=self.started_at,
            ended_at=self.ended_at or utc_now_iso(),
            metrics=self.metrics(),
        )


def _elapsed_ms(start_iso: str) -> float:
    """Milliseconds between an ISO-8601 UTC stamp and now."""
    try:
        start = calendar.timegm(time.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:  # pragma: no cover - defensive
        return 0.0
    return (time.time() - start) * 1000


class TraceStore:
    """In-memory index with optional JSON persistence under ``runs/``."""

    def __init__(self, runs_dir: str | Path | None = None, *, persist: bool = True) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir is not None else get_settings().runs_dir()
        self.persist = persist
        self._traces: dict[str, Trace] = {}
        self._lock = threading.Lock()

    def save(self, trace: Trace) -> None:
        if self.persist:
            path = self.runs_dir / f"{trace.task_id}.trace.json"
            try:
                atomic_write_text(
                    path,
                    json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2),
                )
            except Exception as exc:
                raise PersistenceError("trace could not be committed") from exc
        with self._lock:
            self._traces[trace.task_id] = trace

    def get(self, task_id: str) -> Trace | None:
        trace = self._traces.get(task_id)
        if trace is not None:
            return trace
        path = self.runs_dir / f"{task_id}.trace.json"
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise PersistenceError("trace could not be read") from exc
            try:
                trace = Trace.model_validate(json.loads(raw))
            except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
                raise PersistenceCorruptionError("trace is corrupted") from exc
            with self._lock:
                self._traces[task_id] = trace
            return trace
        return None

    def all_task_ids(self) -> list[str]:
        ids = set(self._traces)
        if self.runs_dir.exists():
            ids.update(p.name.split(".")[0] for p in self.runs_dir.glob("*.trace.json"))
        return sorted(ids)
