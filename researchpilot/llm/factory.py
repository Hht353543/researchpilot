"""Provider + runner construction."""

from __future__ import annotations

from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import LLMProvider
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.llm.openai_provider import OpenAICompatibleProvider
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.observability.trace import Tracer


def build_provider(settings: Settings | None = None, provider: str | None = None) -> LLMProvider:
    settings = settings or get_settings()
    name = provider or settings.provider
    if name == "mock":
        return MockLLMProvider(settings)
    if name == "openai":
        return OpenAICompatibleProvider(settings)
    raise ValueError(f"unknown provider: {name}")


def build_structured_runner(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    tracer: Tracer | None = None,
) -> StructuredLLMRunner:
    settings = settings or get_settings()
    resolved = provider or build_provider(settings)
    return StructuredLLMRunner(
        resolved,
        tracer=tracer,
        max_repair_retries=settings.max_retries,
        token_budget=settings.token_budget,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )
