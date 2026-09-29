"""A real model writes the subtask intent as a sentence; the schema must cope.

A live deepseek-chat run failed 35/35 plans because ``Subtask.intent`` was a
strict ``Literal`` and the model answered with phrases like
「从内部知识库中确认……」. Pydantic rejected the plan, the planner fell back to
its deterministic plan, and the offline suite never noticed because the mock
provider only ever emits enum values.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from researchpilot.agents.base import build_runtime
from researchpilot.agents.planner import PlannerAgent
from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMResponse
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import INTENT_VALUES, ResearchPlan, Subtask, normalize_intent


def _subtask(intent: object) -> Subtask:
    return Subtask(
        id="S1",
        question="q",
        intent=intent,  # type: ignore[arg-type]
        tools=[],
        expected_output="out",
    )


@pytest.mark.parametrize("value", INTENT_VALUES)
def test_enum_values_pass_through_untouched(value: str) -> None:
    assert _subtask(value).intent == value
    assert normalize_intent(value) == value


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("从内部知识库中确认工具注册表的字段", "knowledge_search"),
        ("在知识库中检索相关证据", "knowledge_search"),
        ("Retrieve the relevant passages from the knowledge base", "knowledge_search"),
        ("用 MCP 工具网关获取研究上下文", "mcp"),
        ("通过 MCP server 调用 search_web", "mcp"),
        ("检索公开网页并交叉核对", "web_search"),
        ("Search the web for recent benchmarks", "web_search"),
        ("读取文档原文并摘录引用", "document_reader"),
        ("计算 2024 到 2025 的增长率", "calculation"),
        ("Compute the compound growth rate", "calculation"),
        ("交叉验证并形成结论", "synthesis"),
        ("总结各子任务的发现", "synthesis"),
        ("Synthesise the findings into a conclusion", "synthesis"),
    ],
)
def test_natural_language_intents_are_normalised(written: str, expected: str) -> None:
    assert _subtask(written).intent == expected
    assert normalize_intent(written) == expected


@pytest.mark.parametrize("value", ["", "   ", "???", None, 42])
def test_unrecognised_intents_fall_back_to_knowledge_search(value: object) -> None:
    assert normalize_intent(value) == "knowledge_search"


def test_a_whole_plan_with_free_text_intents_validates() -> None:
    """The blocker was at the plan level, so check the plan, not just the field."""
    plan = ResearchPlan.model_validate(
        {
            "objective": "分析 AI Agent 的应用趋势",
            "subtasks": [
                {
                    "id": "S1",
                    "question": "现有哪些落地案例？",
                    "intent": "从内部知识库中确认已有案例",
                    "tools": ["knowledge_search"],
                    "expected_output": "案例清单",
                    "priority": 3,
                    "depends_on": [],
                },
                {
                    "id": "S2",
                    "question": "还需要什么公开资料？",
                    "intent": "检索公开网页补充最新信息",
                    "tools": ["web_search"],
                    "expected_output": "补充来源",
                    "priority": 2,
                    "depends_on": ["S1"],
                },
            ],
            "requires_knowledge_base": True,
        }
    )
    assert [subtask.intent for subtask in plan.subtasks] == ["knowledge_search", "web_search"]


def test_json_schema_still_advertises_the_enum() -> None:
    """Prompts keep the strict vocabulary; only the parser is tolerant."""
    schema = ResearchPlan.model_json_schema()["$defs"]["Subtask"]["properties"]["intent"]
    assert schema.get("enum") == list(INTENT_VALUES)


class _FreeTextIntentProvider(MockLLMProvider):
    """Stands in for a real model that writes intents as sentences."""

    def model_name(self) -> str:
        return "stub-free-text-intents"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        **_: Any,
    ) -> LLMResponse:
        payload = {
            "objective": "分析 AI Agent 的应用趋势",
            "subtasks": [
                {
                    "id": "a",
                    "question": "知识库里有哪些落地案例？",
                    "intent": "从内部知识库中确认落地案例",
                    "tools": ["knowledge_search"],
                    "expected_output": "案例清单",
                    "priority": 3,
                    "depends_on": [],
                },
                {
                    "id": "b",
                    "question": "网页上有哪些最新进展？",
                    "intent": "检索公开网页补充最新信息",
                    "tools": ["web_search"],
                    "expected_output": "补充来源",
                    "priority": 2,
                    "depends_on": ["a"],
                },
            ],
            "requires_knowledge_base": True,
            "requires_web": True,
            "requires_mcp": False,
            "success_criteria": ["覆盖度"],
            "max_iterations": 2,
            "rationale": "先查内部资料，再补公开资料",
        }
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model=self.model_name(),
            attempts=1,
        )


def test_planner_keeps_the_model_plan_instead_of_falling_back(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    """The live-run blocker: free-text intents used to fail the whole plan."""
    question = "分析 AI Agent 的应用趋势"
    runtime = build_runtime(
        task_id="free-text-intent",
        question=question,
        settings=settings,
        provider=_FreeTextIntentProvider(settings),
        knowledge_base=knowledge_base,
    )
    plan = PlannerAgent(runtime).run(question)

    assert [subtask.intent for subtask in plan.subtasks][:2] == ["knowledge_search", "web_search"]
    assert runtime.errors == [], "the plan must validate without the planner fallback"
    span = next(span for span in runtime.tracer.spans if span.name == "PlannerAgent")
    assert span.output["attempts"] == 1, "no repair retries: the schema accepted the first answer"
