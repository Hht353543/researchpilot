"""Spec §3 requirements that must be proven by execution, not by class names:

1. the plan really drives which tools run;
2. the Critic really can trigger another research iteration;
3. the Agent state used downstream is the plan's state.
"""

from __future__ import annotations

from researchpilot.agents.base import build_runtime
from researchpilot.agents.critic import CriticAgent
from researchpilot.agents.researcher import ResearchAgent
from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import (
    CritiqueReport,
    EvidenceBundle,
    ResearchPlan,
    ResearchRequest,
    Subtask,
    VerificationReport,
)


def _plan(*, tools: list[str], intent: str = "web_search") -> ResearchPlan:
    return ResearchPlan(
        objective="验证计划是否驱动执行",
        subtasks=[
            Subtask(
                id="S1",
                question="只允许调用计划里声明的工具",
                intent=intent,  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
                expected_output="证据",
                priority=5,
            ),
            Subtask(
                id="S2",
                question="综合结论",
                intent="synthesis",
                tools=[],
                expected_output="结论",
                depends_on=["S1"],
            ),
        ],
        max_iterations=1,
    )


def _runtime(settings: Settings, knowledge_base: KnowledgeBase) -> object:
    return build_runtime(
        task_id="plan-driven",
        question="计划驱动执行",
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
    )


def test_executed_tools_come_from_the_plan(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """A plan that only declares web_search must not secretly call knowledge_search."""
    runtime = _runtime(settings, knowledge_base)
    ResearchAgent(runtime).run(_plan(tools=["web_search"]))
    used = {span.tool for span in runtime.tracer.spans if span.kind in {"tool", "mcp"} and span.tool}
    assert used == {"web_search"}, f"unexpected tools executed: {used}"


def test_executed_tools_follow_a_different_plan(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """Same agent, different plan -> different tools (proves plan-driven dispatch)."""
    runtime = _runtime(settings, knowledge_base)
    ResearchAgent(runtime).run(_plan(tools=["metadata"], intent="knowledge_search"))
    used = {span.tool for span in runtime.tracer.spans if span.kind in {"tool", "mcp"} and span.tool}
    assert used == {"metadata"}, f"unexpected tools executed: {used}"


def test_plan_subtask_ids_flow_into_evidence(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    runtime = _runtime(settings, knowledge_base)
    bundle = ResearchAgent(runtime).run(_plan(tools=["knowledge_search"], intent="knowledge_search"))
    assert bundle.evidence, "knowledge_search plan should yield evidence"
    assert {item.subtask_id for item in bundle.evidence} == {"S1"}
    assert all(item.tool == "knowledge_search" for item in bundle.evidence)


def test_critic_can_trigger_another_iteration(
    settings: Settings, knowledge_base: KnowledgeBase, monkeypatch
) -> None:
    """Spec §3.8: when the Critic asks for more research, the pipeline really loops."""
    calls = {"count": 0}
    original = CriticAgent.run

    def fake_run(
        self: CriticAgent,
        plan: ResearchPlan,
        bundle: EvidenceBundle,
        verification: VerificationReport,
    ) -> CritiqueReport:
        calls["count"] += 1
        report = original(self, plan, bundle, verification)
        if calls["count"] == 1:
            return report.model_copy(
                update={
                    "needs_more_research": True,
                    "follow_up_queries": ["MCP 的传输方式有哪些？"],
                }
            )
        return report.model_copy(update={"needs_more_research": False, "follow_up_queries": []})

    monkeypatch.setattr(CriticAgent, "run", fake_run)
    loop_settings = settings.model_copy(update={"max_iterations": 2})
    pipeline = ResearchPipeline(
        settings=loop_settings,
        provider=MockLLMProvider(loop_settings),
        knowledge_base=knowledge_base,
    )
    result = pipeline.run(ResearchRequest(question="MCP 的传输方式有哪些？"))

    assert calls["count"] >= 2, "critic must be consulted again after the follow-up round"
    assert result.evidence.iterations >= 2, "the follow-up evidence must be merged"
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None
    iteration_spans = [span for span in trace.spans if span.name.startswith("pipeline.iteration[")]
    assert iteration_spans, "the pipeline must emit an iteration span for the follow-up round"


def test_iteration_is_bounded_by_max_iterations(
    settings: Settings, knowledge_base: KnowledgeBase, monkeypatch
) -> None:
    """The loop must stop at max_iterations even if the Critic never relents."""
    always_more = CritiqueReport(
        issues=[],
        needs_more_research=True,
        follow_up_queries=["再查一次"],
        coverage={},
        overall_assessment="keep going",
    )

    def always_demand_more(
        self: CriticAgent,
        plan: ResearchPlan,
        bundle: EvidenceBundle,
        verification: VerificationReport,
    ) -> CritiqueReport:
        return always_more

    monkeypatch.setattr(CriticAgent, "run", always_demand_more)
    bounded = settings.model_copy(update={"max_iterations": 2})
    pipeline = ResearchPipeline(
        settings=bounded, provider=MockLLMProvider(bounded), knowledge_base=knowledge_base
    )
    result = pipeline.run(ResearchRequest(question="MCP 的传输方式有哪些？"))
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None
    iteration_spans = [span for span in trace.spans if span.name.startswith("pipeline.iteration[")]
    assert len(iteration_spans) == 1, "exactly one follow-up round is allowed for max_iterations=2"
    assert result.evidence.iterations == 2
