"""PlannerAgent contract: structured plan, validation, fallback, state propagation."""

from __future__ import annotations

from researchpilot.agents.base import build_runtime
from researchpilot.agents.planner import PlannerAgent
from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchPlan, Subtask


def _agent(settings: Settings, knowledge_base: KnowledgeBase, question: str) -> PlannerAgent:
    runtime = build_runtime(
        task_id="planner-task",
        question=question,
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
    )
    return PlannerAgent(runtime)


def test_planner_produces_structured_plan_and_stores_it_in_memory(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    agent = _agent(settings, knowledge_base, "分析 AI Agent 的趋势、技术路线与优缺点")
    plan = agent.run("分析 AI Agent 的趋势、技术路线与优缺点")

    assert isinstance(plan, ResearchPlan)
    assert plan.subtasks
    assert isinstance(plan.subtasks[0], Subtask)
    # Plan state lives in working memory, not in a formatted string.
    assert agent.runtime.memory.working.plan is not None
    assert agent.runtime.memory.working.subtasks == plan.subtasks
    assert agent.runtime.memory.working.objective == plan.objective
    # Every tool named by the plan exists in the registry.
    for subtask in plan.subtasks:
        for tool in subtask.tools:
            assert agent.runtime.tools.has(tool), f"plan references unknown tool {tool}"
    # Short-term memory records the user question and the planner note.
    assert agent.runtime.memory.short_term.contains("AI Agent")
    assert any(item.agent == "PlannerAgent" for item in agent.runtime.memory.short_term.items)


def test_planner_emits_trace_span(knowledge_base: KnowledgeBase, settings: Settings) -> None:
    agent = _agent(settings, knowledge_base, "MCP 的核心方法有哪些？")
    agent.run("MCP 的核心方法有哪些？")
    spans = [span for span in agent.runtime.tracer.spans if span.name == "PlannerAgent"]
    assert spans, "PlannerAgent must emit an agent span"
    assert spans[0].kind == "agent"
    assert spans[0].latency_ms >= 0
    assert spans[0].output.get("subtasks")


def test_normalise_renumbers_ids_and_drops_unknown_tools(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    """A provider that violates the tool enum must not break the pipeline.

    ``Subtask.tools`` is a Literal, so the structured-output validator already
    rejects unknown tools; this test simulates a model that bypassed validation
    (``model_construct``) to prove the planner's own guard also drops them.
    """
    agent = _agent(settings, knowledge_base, "任意问题")
    raw = ResearchPlan.model_construct(
        objective="任意问题",
        subtasks=[
            Subtask.model_construct(
                id="weird-id",
                question="第一个子任务",
                intent="knowledge_search",
                tools=["not_a_tool"],
                expected_output="x",
                priority=5,
                depends_on=[],
            ),
            Subtask.model_construct(
                id="dup",
                question="第二个子任务",
                intent="web_search",
                tools=["knowledge_search"],
                expected_output="y",
                priority=4,
                depends_on=[],
            ),
        ],
        requires_knowledge_base=True,
        requires_web=True,
        requires_mcp=False,
        success_criteria=[],
        max_iterations=9,
        rationale="",
    )
    normalised = agent._normalise(raw, agent.runtime.tools.names(), max_iterations=2)

    assert [s.id for s in normalised.subtasks] == ["S1", "S2", "S3"]
    assert normalised.subtasks[0].tools == ["knowledge_search"], "unknown tool must be dropped"
    assert normalised.subtasks[0].intent == "knowledge_search"
    assert normalised.subtasks[-1].intent == "synthesis", "synthesis subtask must be ensured"
    assert normalised.subtasks[-1].depends_on == ["S1", "S2"]
    assert normalised.max_iterations == 2, "iterations must be clamped to the runtime limit"


def test_normalise_maps_intent_to_real_tool_when_tools_missing(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    agent = _agent(settings, knowledge_base, "任意问题")
    raw = ResearchPlan(
        objective="任意问题",
        subtasks=[
            Subtask(
                id="S1",
                question="用 MCP 网关查询",
                intent="mcp",
                tools=[],
                expected_output="x",
            )
        ],
    )
    available = ["knowledge_search", "mcp_research_context"]
    normalised = agent._normalise(raw, available, max_iterations=1)
    assert normalised.subtasks[0].tools == ["mcp_research_context"], (
        "intent 'mcp' must map to the mcp_research_context tool, not to a tool named 'mcp'"
    )

    fallback = agent._normalise(raw, ["knowledge_search"], max_iterations=1)
    assert fallback.subtasks[0].tools == ["knowledge_search"]


def test_fallback_plan_only_uses_available_tools(settings: Settings) -> None:
    plan = PlannerAgent._fallback_plan("问题", ["knowledge_search"], max_iterations=1)
    assert plan.subtasks
    assert plan.subtasks[-1].intent == "synthesis"
    for subtask in plan.subtasks:
        assert set(subtask.tools) <= {"knowledge_search"}
    assert plan.requires_web is False
    assert plan.requires_mcp is False


def test_fallback_plan_when_tool_registry_is_empty() -> None:
    plan = PlannerAgent._fallback_plan("问题", [], max_iterations=2)
    assert plan.subtasks[-1].intent == "synthesis"
    assert all(not subtask.tools for subtask in plan.subtasks[:-1])
