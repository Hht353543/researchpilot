"""PlannerAgent: decompose the question into a structured, tool-aware plan."""

from __future__ import annotations

from typing import Any, Literal

from researchpilot.agents.base import BaseAgent
from researchpilot.llm.prompts import PLANNER_SYSTEM, planner_user
from researchpilot.schemas import ResearchPlan, Subtask, ToolName
from researchpilot.utils import truncate

KNOWN_INTENTS = {
    "knowledge_search",
    "web_search",
    "document_reader",
    "mcp",
    "calculation",
    "synthesis",
}
Intent = Literal["knowledge_search", "web_search", "document_reader", "mcp", "calculation", "synthesis"]
_INTENT_TO_TOOL: dict[str, ToolName] = {
    "knowledge_search": "knowledge_search",
    "web_search": "web_search",
    "document_reader": "document_reader",
    "mcp": "mcp_research_context",
}


class PlannerAgent(BaseAgent):
    name = "PlannerAgent"

    def run(self, question: str, *, max_iterations: int | None = None) -> ResearchPlan:
        runtime = self.runtime
        iterations = max_iterations or runtime.settings.max_iterations
        with self.span(input={"question": question}) as span:
            memory_context = self._safe(lambda: runtime.memory.recall_context(question, limit=3), "")
            kb_summary = self._safe(lambda: runtime.knowledge_base.stats(), {"error": "unavailable"})
            runtime.memory.note("user", question)
            attempts = 0
            try:
                result = runtime.llm.run(
                    ResearchPlan,
                    agent=self.name,
                    system=PLANNER_SYSTEM,
                    user=planner_user(question, runtime.tools.catalogue(), kb_summary),
                    hints={
                        "question": question,
                        "tools": runtime.tools.names(),
                        "max_iterations": iterations,
                        "memory_context": memory_context,
                    },
                    purpose="planner",
                    max_tokens=1500,
                )
                plan: ResearchPlan = result.value
                attempts = result.attempts
            except Exception as exc:
                runtime.errors.append(f"planner: {type(exc).__name__}: {exc}")
                plan = self._fallback_plan(question, runtime.tools.names(), iterations)
            plan = self._normalise(plan, runtime.tools.names(), iterations)
            runtime.memory.set_plan(plan)
            self.note(f"plan: {len(plan.subtasks)} subtasks")
            runtime.tracer.end_span(
                span,
                output={
                    "subtasks": [
                        {"id": s.id, "intent": s.intent, "tools": s.tools, "question": s.question}
                        for s in plan.subtasks
                    ],
                    "attempts": attempts,
                },
            )
        return plan

    def _safe(self, action: Any, fallback: Any) -> Any:
        """Infrastructure reads must never abort planning (degrade instead)."""
        try:
            return action()
        except Exception as exc:
            self.runtime.errors.append(f"planner: {type(exc).__name__}: {exc}")
            return fallback

    # -- validation -------------------------------------------------------- #
    def _normalise(self, plan: ResearchPlan, available: list[str], max_iterations: int) -> ResearchPlan:
        subtasks: list[Subtask] = []
        seen_ids: set[str] = set()
        for index, subtask in enumerate(list(plan.subtasks)[:8], start=1):
            new_id = f"S{index}"
            if new_id in seen_ids:  # pragma: no cover - sequential ids
                continue
            seen_ids.add(new_id)
            intent: str = subtask.intent if subtask.intent in KNOWN_INTENTS else "knowledge_search"
            tools = [tool for tool in subtask.tools if tool in available]
            if intent in {"knowledge_search", "web_search", "document_reader", "mcp"} and not tools:
                mapped: ToolName = _INTENT_TO_TOOL.get(intent, "knowledge_search")
                tools = [mapped] if mapped in available else ["knowledge_search"]
            dependencies = [dep for dep in subtask.depends_on if dep in seen_ids]
            subtasks.append(
                subtask.model_copy(
                    update={
                        "id": new_id,
                        "intent": intent,
                        "tools": tools,
                        "depends_on": dependencies,
                        "question": truncate(subtask.question, 400),
                    }
                )
            )
        if not subtasks:
            return self._fallback_plan(plan.objective, available, max_iterations)
        if not any(s.intent == "synthesis" for s in subtasks):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{plan.objective} —— 交叉验证并形成结论",
                    intent="synthesis",
                    tools=[],
                    expected_output="经过验证的结论与引用绑定",
                    priority=5,
                    depends_on=[s.id for s in subtasks],
                )
            )
        return plan.model_copy(
            update={
                "subtasks": subtasks,
                "max_iterations": max(1, min(plan.max_iterations, max_iterations)),
                "objective": truncate(plan.objective or "未命名研究任务", 400),
            }
        )

    @staticmethod
    def _fallback_plan(question: str, available: list[str], max_iterations: int) -> ResearchPlan:
        """Deterministic plan used when the LLM path fails (a task never gets stuck)."""

        def pick(tool: ToolName) -> list[ToolName]:
            return [tool] if tool in available else []

        subtasks: list[Subtask] = [
            Subtask(
                id="S1",
                question=f"{question} —— 界定概念与范围",
                intent="knowledge_search",
                tools=pick("knowledge_search"),
                expected_output="核心概念与范围界定",
                priority=5,
            ),
            Subtask(
                id="S2",
                question=f"{question} —— 检索事实与案例证据",
                intent="web_search",
                tools=pick("web_search") + pick("mcp_research_context"),
                expected_output="事实与案例证据",
                priority=4,
            ),
            Subtask(
                id="S3",
                question=f"{question} —— 归纳优缺点与风险",
                intent="knowledge_search",
                tools=pick("knowledge_search") + pick("metadata"),
                expected_output="优缺点与风险清单",
                priority=4,
            ),
            Subtask(
                id="S4",
                question=f"{question} —— 交叉验证并形成结论",
                intent="synthesis",
                tools=[],
                expected_output="验证后的结论",
                priority=5,
                depends_on=["S1", "S2", "S3"],
            ),
        ]
        return ResearchPlan(
            objective=question,
            subtasks=subtasks,
            requires_knowledge_base=True,
            requires_web="web_search" in available,
            requires_mcp="mcp_research_context" in available,
            success_criteria=["每个子任务都有证据或明确缺口", "结论绑定引用"],
            max_iterations=max_iterations,
            rationale="degraded: deterministic fallback plan",
        )
