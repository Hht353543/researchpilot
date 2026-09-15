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


def token_budget_for(settings: Settings, provider_name: str) -> int:
    """Per-task token ceiling: the offline budget for mock, the live one otherwise.

    A real model spends far more per task than the scripted mock (58.9k average and
    79.7k worst case against an 80k ceiling in the 2026-09-15 deepseek run), so the
    two providers get separate budgets and CI keeps regressing the offline one.
    """
    return settings.token_budget if provider_name == "mock" else settings.token_budget_live


def build_structured_runner(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    tracer: Tracer | None = None,
) -> StructuredLLMRunner:
    settings = settings or get_settings()
    resolved = provider or build_provider(settings)
    provider_name = getattr(resolved, "name", "") or settings.provider
    return StructuredLLMRunner(
        resolved,
        tracer=tracer,
        max_repair_retries=settings.max_retries,
        token_budget=token_budget_for(settings, provider_name),
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
    )
