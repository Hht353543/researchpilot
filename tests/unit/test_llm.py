"""LLM layer: provider abstraction, structured output validation and repair."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from researchpilot.llm.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    StructuredOutputError,
)
from researchpilot.llm.mock_provider import MockLLMProvider, split_paragraphs
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.observability.trace import Tracer
from researchpilot.schemas import ResearchPlan, TokenUsage


class Answer(BaseModel):
    value: int
    note: str = ""


class ScriptedProvider(LLMProvider):
    """Returns scripted answers so validation/repair logic can be tested."""

    name = "scripted"

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0
        self.last_parameters: dict[str, float | int | None] = {}

    def model_name(self) -> str:
        return "scripted-model"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> LLMResponse:
        self.last_parameters = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
        }
        text = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return LLMResponse(
            text=text,
            model=self.model_name(),
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def test_structured_runner_parses_and_validates() -> None:
    provider = ScriptedProvider(['{"value": 7, "note": "ok"}'])
    tracer = Tracer("t", "q")
    runner = StructuredLLMRunner(provider, tracer=tracer)
    result = runner.run(Answer, agent="Test", system="s", user="u")
    assert isinstance(result.value, Answer)
    assert result.value.value == 7
    assert runner.usage.total_tokens == 15
    assert any(span.kind == "llm" for span in tracer.spans)


def test_structured_runner_passes_explicit_generation_parameters_including_zero() -> None:
    provider = ScriptedProvider(['{"value": 7}'])
    runner = StructuredLLMRunner(
        provider,
        temperature=0.0,
        max_tokens=999,
        max_tokens_override=321,
        presence_penalty=0.0,
        frequency_penalty=-0.5,
    )

    runner.run(Answer, agent="Test", system="s", user="u", max_tokens=3_000)

    assert provider.last_parameters == {
        "temperature": 0.0,
        "max_tokens": 321,
        "presence_penalty": 0.0,
        "frequency_penalty": -0.5,
    }

    provider.answers = ["plain text"]
    runner.run_text(agent="Test", system="s", user="u", max_tokens=4_000)
    assert provider.last_parameters == {
        "temperature": 0.0,
        "max_tokens": 321,
        "presence_penalty": 0.0,
        "frequency_penalty": -0.5,
    }


def test_structured_runner_repairs_invalid_json() -> None:
    provider = ScriptedProvider(["not json at all", '{"value": 3}'])
    tracer = Tracer("t", "q")
    runner = StructuredLLMRunner(provider, max_repair_retries=2, tracer=tracer)
    result = runner.run(Answer, agent="Test", system="s", user="u")
    assert result.value.value == 3
    assert result.attempts == 2
    assert result.errors and "attempt 1" in result.errors[0]
    assert any(span.kind == "retry" for span in tracer.spans)


def test_structured_runner_raises_after_exhausting_retries() -> None:
    provider = ScriptedProvider(["nope"])
    runner = StructuredLLMRunner(provider, max_repair_retries=1)
    with pytest.raises(StructuredOutputError):
        runner.run(Answer, agent="Test", system="s", user="u")


def test_token_budget_is_enforced() -> None:
    provider = ScriptedProvider(['{"value": 1}'])
    runner = StructuredLLMRunner(provider, token_budget=10)
    with pytest.raises(LLMError):
        runner.run(Answer, agent="Test", system="s", user="u")


def test_token_budget_accumulates_across_lifecycle_checkpoints() -> None:
    provider = ScriptedProvider(['{"value": 1}'])
    runner = StructuredLLMRunner(provider, token_budget=25)
    runner.run(Answer, agent="Test", system="s", user="u")
    with pytest.raises(LLMError):
        runner.run(Answer, agent="Test", system="s", user="u")
    assert runner.usage.total_tokens == 30


def test_mock_provider_produces_schema_valid_plan() -> None:
    provider = MockLLMProvider()
    response = provider.complete(
        [ChatMessage(role="user", content="q")],
        response_schema=ResearchPlan,
        hints={"question": "分析 AI Agent 在企业落地趋势、技术路线与优缺点", "tools": ["knowledge_search"]},
        purpose="planner",
    )
    plan = ResearchPlan.model_validate_json(response.text)
    assert plan.subtasks
    assert plan.subtasks[-1].intent == "synthesis"
    assert all(subtask.tools for subtask in plan.subtasks[:-1])


def test_mock_provider_rejects_unknown_schema() -> None:
    provider = MockLLMProvider()
    with pytest.raises(LLMError):
        provider.complete(
            [ChatMessage(role="user", content="q")],
            response_schema=Answer,
            purpose="unknown",
        )


def test_mock_provider_rewrites_query() -> None:
    provider = MockLLMProvider()
    response = provider.complete(
        [ChatMessage(role="user", content="q")],
        purpose="query_rewrite",
        hints={"query": "RAG 重排", "known_topics": ["rerank", "retrieval"]},
    )
    assert "RAG 重排" in response.text


def test_split_paragraphs_drops_heading_context() -> None:
    content = "文档标题 > 小节\n\n第一段内容足够长可以成为证据候选。\n\n第二段内容同样足够长。"
    paragraphs = split_paragraphs(content)
    assert paragraphs
    assert all(">" not in paragraph for paragraph in paragraphs)
