"""End-to-end guard for the failure signature a real model produced.

The deepseek-chat baseline failed for reasons the offline mock can never produce:
the planner answered with natural-language intents, and the writer put its
citations in the prose while leaving the ``evidence_ids`` arrays empty. Both were
fixed at the unit level; this test runs the whole pipeline with a provider that
behaves that way, so a regression anywhere in the chain shows up here.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMResponse
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest

PLANNER_PAYLOAD: dict[str, Any] = {
    "objective": "工具注册表需要声明哪些字段？",
    "subtasks": [
        {
            "id": "a",
            "question": "知识库里怎么描述工具契约？",
            "intent": "从内部知识库中确认工具注册表的字段要求",
            "tools": ["knowledge_search"],
            "expected_output": "字段清单与出处",
            "priority": 3,
            "depends_on": [],
        },
        {
            "id": "b",
            "question": "这些字段怎么用？",
            "intent": "综合已有证据形成结论",
            "tools": [],
            "expected_output": "结论",
            "priority": 2,
            "depends_on": ["a"],
        },
    ],
    "requires_knowledge_base": True,
    "requires_web": False,
    "requires_mcp": False,
    "success_criteria": ["字段清单有出处"],
    "max_iterations": 1,
    "rationale": "先查知识库再总结",
}

# Citations live in the prose; the arrays the schema asks for are left empty.
WRITER_PAYLOAD: dict[str, Any] = {
    "title": "工具注册表字段（模型撰写）",
    "executive_summary": "工具注册表要求每个工具声明契约字段 [E1]。",
    "conclusions": [{"statement": "每个工具都要声明 schema、权限、超时与重试策略 [E1]", "evidence_ids": []}],
    "sections": [
        {"heading": "字段要求", "body": "知识库说明这些字段用于约束调用 [E1]。", "evidence_ids": []}
    ],
    "recommendations": [],
    "limitations": [],
    "dropped_citations": [],
    "markdown": "",
}


class _LiveModelShapedProvider(MockLLMProvider):
    """Delegates to the mock, except for the two calls a real model got wrong."""

    def model_name(self) -> str:
        return "stub-live-model-shaped"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        **kwargs: Any,
    ) -> LLMResponse:
        payload = {"planner": PLANNER_PAYLOAD, "writer": WRITER_PAYLOAD}.get(purpose)
        if payload is not None:
            return LLMResponse(
                text=json.dumps(payload, ensure_ascii=False), model=self.model_name(), attempts=1
            )
        return super().complete(
            messages, response_schema=response_schema, hints=hints, purpose=purpose, **kwargs
        )


def _run(settings: Settings, knowledge_base: KnowledgeBase):
    pipeline = ResearchPipeline(
        settings=settings,
        provider=_LiveModelShapedProvider(settings),
        knowledge_base=knowledge_base,
    )
    return pipeline.run(ResearchRequest(question="工具注册表需要声明哪些字段？"))


def test_pipeline_keeps_a_model_plan_with_natural_language_intents(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    result = _run(settings, knowledge_base)
    assert result.plan is not None and result.plan.subtasks
    assert not [error for error in result.errors if error.startswith("planner:")], result.errors
    assert [subtask.intent for subtask in result.plan.subtasks][:2] == ["knowledge_search", "synthesis"]


def test_pipeline_keeps_a_model_report_that_cites_inline(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    result = _run(settings, knowledge_base)
    assert result.report is not None
    assert not [error for error in result.errors if "extractive fallback" in error], (
        "the model report was replaced by the fallback writer"
    )
    assert result.report.title == "工具注册表字段（模型撰写）"
    assert result.report.conclusions and result.report.conclusions[0].evidence_ids
    assert "unbound" not in result.report.markdown.lower()


def test_pipeline_still_finishes_with_a_report_and_sources(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    result = _run(settings, knowledge_base)
    assert result.status in {"succeeded", "degraded"}
    assert result.report is not None and result.report.markdown
    assert result.evidence.sources
    assert result.report.dropped_citations == []
