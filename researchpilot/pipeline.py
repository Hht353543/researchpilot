"""ResearchPipeline: orchestrates Planner -> Researcher -> Verifier -> Critic -> Writer."""

from __future__ import annotations

import threading
from typing import Any

from researchpilot.agents.base import ResearchRuntime, build_runtime
from researchpilot.agents.critic import CriticAgent
from researchpilot.agents.planner import PlannerAgent
from researchpilot.agents.researcher import ResearchAgent
from researchpilot.agents.verifier import VerifierAgent
from researchpilot.agents.writer import WriterAgent
from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import BudgetExceededError, LLMConfigError, LLMProvider
from researchpilot.llm.factory import build_provider
from researchpilot.observability.trace import Tracer, TraceStore, tracer_scope
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import (
    EvidenceBundle,
    ResearchRequest,
    ResearchResult,
    ResearchSettings,
    TaskMetrics,
    TaskStatus,
)
from researchpilot.store import ResultStore
from researchpilot.tools.registry import ToolRegistry
from researchpilot.utils import new_id, utc_now_iso


class ResearchPipeline:
    """End-to-end multi-agent deep research."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: LLMProvider | None = None,
        knowledge_base: KnowledgeBase | None = None,
        tools: ToolRegistry | None = None,
        trace_store: TraceStore | None = None,
        result_store: ResultStore | None = None,
        mcp_client: Any = None,
        tool_overrides: Any = None,
        tool_policy: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider
        self.knowledge_base = knowledge_base or KnowledgeBase.load_or_create(self.settings)
        self.tools = tools
        self.trace_store = trace_store or TraceStore(self.settings.runs_dir())
        self.result_store = result_store or ResultStore(self.settings.runs_dir())
        self.mcp_client = mcp_client
        self.tool_overrides = tool_overrides
        self.tool_policy = tool_policy

    # -- helpers ----------------------------------------------------------- #
    def _request_settings(self, request: ResearchRequest) -> Settings:
        merged = request.settings
        updates: dict[str, Any] = {}
        if merged.model:
            updates["model"] = merged.model
        for field in (
            "temperature",
            "presence_penalty",
            "frequency_penalty",
            "max_tokens",
            "top_k",
            "max_iterations",
            "token_budget",
        ):
            value = getattr(merged, field)
            if value is not None:
                updates[field] = value
        return self.settings.model_copy(update=updates) if updates else self.settings

    def _build_runtime(
        self, task_id: str, question: str, settings: Settings, tracer: Tracer, request: ResearchRequest
    ) -> ResearchRuntime:
        provider = self.provider or build_provider(settings)
        mcp_client = self.mcp_client
        if mcp_client is None and settings.mcp_transport == "inprocess":
            from researchpilot.mcp.client import InProcessMcpClient
            from researchpilot.mcp.server import McpServer

            server = McpServer(settings, knowledge_base=self.knowledge_base)
            mcp_client = InProcessMcpClient(server)
        runtime = build_runtime(
            task_id=task_id,
            question=question,
            settings=settings,
            tracer=tracer,
            provider=provider,
            knowledge_base=self.knowledge_base,
            tools=self.tools,
            mcp_client=mcp_client,
            policy=self.tool_policy,
        )
        if self.tool_overrides:
            overrides = (
                self.tool_overrides(runtime.tools) if callable(self.tool_overrides) else self.tool_overrides
            )
            for name, tool in overrides.items():
                runtime.tools.register(tool)
                if not runtime.tools.has(name):  # pragma: no cover - defensive
                    raise RuntimeError(f"tool override for unknown tool {name}")
        runtime.scratch["max_sources"] = request.max_sources
        return runtime

    # -- main entry point -------------------------------------------------- #
    def run(self, request: ResearchRequest, *, task_id: str | None = None) -> ResearchResult:
        task_id = task_id or new_id("task")
        settings = self._request_settings(request)
        tracer = Tracer(task_id, request.question, price_table=settings.price_table())
        result = ResearchResult(
            task_id=task_id,
            trace_id=tracer.trace_id,
            status="running",
            question=request.question,
            settings=ResearchSettings(**request.settings.model_dump()),
            created_at=utc_now_iso(),
        )
        runtime: ResearchRuntime | None = None
        try:
            runtime = self._build_runtime(task_id, request.question, settings, tracer, request)
            plan, bundle, verification, critique, report = self._execute(runtime, request, tracer=tracer)
            result.plan = plan
            result.evidence = bundle
            result.verification = verification
            result.critique = critique
            result.report = report
            result.errors = list(runtime.errors)
            result.status = self._status(runtime, bundle)
            if report is not None and result.status != "failed":
                try:
                    runtime.memory.persist_task_result(
                        objective=request.question,
                        conclusions=[(c.statement, c.confidence) for c in report.conclusions],
                    )
                except Exception as exc:
                    # Memory is an enhancement: a failed write must not destroy an
                    # otherwise valid report, but it has to be visible as degradation.
                    result.errors.append(f"memory: {type(exc).__name__}: {exc}")
                    if result.status == "succeeded":
                        result.status = "degraded"
        except BudgetExceededError as exc:
            result.errors.append(f"budget: {exc}")
            result.status = "degraded"
        except LLMConfigError as exc:
            result.errors.append(f"llm_config: {exc}")
            result.status = "failed"
        except Exception as exc:  # pragma: no cover - defensive
            result.errors.append(f"pipeline: {type(exc).__name__}: {exc}")
            result.status = "failed"

        result.completed_at = utc_now_iso()
        metrics = tracer.metrics()
        result.metrics = _with_iterations(metrics, result.evidence.iterations, result.evidence.tool_calls)
        tracer.finish()
        self.trace_store.save(tracer.to_trace())
        self.result_store.save(result)
        return result

    def _execute(
        self, runtime: ResearchRuntime, request: ResearchRequest, *, tracer: Tracer
    ) -> tuple[Any, EvidenceBundle, Any, Any, Any]:
        # Make the tracer ambient as well: nested calls that only receive a
        # ToolContext still produce spans instead of silently losing trace data.
        with tracer_scope(tracer):
            return self._run_agents(runtime, request, tracer=tracer)

    def _run_agents(
        self, runtime: ResearchRuntime, request: ResearchRequest, *, tracer: Tracer
    ) -> tuple[Any, EvidenceBundle, Any, Any, Any]:
        planner = PlannerAgent(runtime)
        researcher = ResearchAgent(runtime)
        verifier = VerifierAgent(runtime)
        critic = CriticAgent(runtime)
        writer = WriterAgent(runtime)

        plan = planner.run(request.question, max_iterations=request.settings.max_iterations)
        bundle = researcher.run(plan, iteration=1)
        verification = verifier.run(plan, bundle)
        critique = critic.run(plan, bundle, verification)

        iteration = 1
        max_iterations = max(plan.max_iterations, 1)
        while critique.needs_more_research and critique.follow_up_queries and iteration < max_iterations:
            iteration += 1
            with tracer.span(
                f"pipeline.iteration[{iteration}]",
                "agent",
                agent="Orchestrator",
                input={"follow_up_queries": critique.follow_up_queries},
            ) as span:
                extra = researcher.run(
                    plan,
                    follow_up_queries=critique.follow_up_queries,
                    iteration=iteration,
                )
                tracer.end_span(
                    span,
                    output={
                        "new_evidence": len(extra.evidence),
                        "follow_ups": len(critique.follow_up_queries),
                    },
                )
            bundle = EvidenceBundle(
                evidence=bundle.evidence + extra.evidence,
                sources=list(runtime.sources.all()),
                gaps=bundle.gaps + extra.gaps,
                iterations=iteration,
                tool_calls=bundle.tool_calls + extra.tool_calls,
            )
            verification = verifier.run(plan, bundle)
            critique = critic.run(plan, bundle, verification)

        report = writer.run(plan, bundle, verification, critique.model_dump())
        return plan, bundle, verification, critique, report

    @staticmethod
    def _status(runtime: ResearchRuntime, bundle: EvidenceBundle) -> TaskStatus:
        """Honest status: no evidence is never 'succeeded'.

        A run that produced zero usable evidence is a *degraded* outcome even when
        every tool technically returned ok=True, otherwise empty-result tasks would
        inflate the success rate.
        """
        if not bundle.evidence and runtime.errors:
            return "failed"
        if runtime.errors or not bundle.evidence:
            return "degraded"
        return "succeeded"

    # -- async submission -------------------------------------------------- #
    def submit(self, request: ResearchRequest) -> str:
        """Run in a background thread; callers poll ``GET /research/{task_id}``."""
        task_id = new_id("task")
        placeholder = ResearchResult(
            task_id=task_id,
            trace_id="pending",
            status="pending",
            question=request.question,
            settings=request.settings,
            created_at=utc_now_iso(),
        )
        self.result_store.save(placeholder)

        def _worker() -> None:
            self.run(request, task_id=task_id)

        threading.Thread(target=_worker, name=f"research-{task_id}", daemon=True).start()
        return task_id


def _with_iterations(metrics: TaskMetrics, iterations: int, tool_calls: int) -> TaskMetrics:
    return metrics.model_copy(
        update={
            "iterations": iterations,
            "tool_calls": max(metrics.tool_calls, tool_calls),
        }
    )
