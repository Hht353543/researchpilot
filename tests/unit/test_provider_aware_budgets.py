"""Budgets invented for the offline mock must not be applied to a real model.

The 2026-09-15 deepseek run failed three tasks on a 60s/90s latency gate while its
fastest task took 61.1s, and five tasks hit the 80k token ceiling with a 79.7k
worst case. Both numbers are calibration for a deterministic script; the live
values are separate, so the offline baseline keeps regressing on the old ones.
"""

from __future__ import annotations

from researchpilot.config import Settings
from researchpilot.evaluation.dataset import Expectations, load_dataset
from researchpilot.evaluation.judge import latency_budget_for
from researchpilot.llm.factory import token_budget_for
from researchpilot.llm.openai_provider import OpenAICompatibleProvider
from researchpilot.observability.trace import Tracer
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest, ResearchSettings


def test_offline_run_keeps_the_offline_latency_budget() -> None:
    exp = Expectations(max_latency_s=60, max_latency_s_live=300)
    assert latency_budget_for(exp, live_model=False) == 60


def test_live_run_uses_the_live_latency_budget() -> None:
    exp = Expectations(max_latency_s=60, max_latency_s_live=300)
    assert latency_budget_for(exp, live_model=True) == 300


def test_live_run_falls_back_when_no_live_budget_is_declared() -> None:
    """A task that only declares the offline budget still gates a live run."""
    exp = Expectations(max_latency_s=60)
    assert latency_budget_for(exp, live_model=True) == 60


def test_task_without_any_budget_is_ungated() -> None:
    assert latency_budget_for(Expectations(), live_model=True) is None


def test_the_three_gated_tasks_declare_both_budgets() -> None:
    gated = {
        task.id: (task.expectations.max_latency_s, task.expectations.max_latency_s_live)
        for task in load_dataset()
        if task.expectations.max_latency_s
    }
    assert gated == {
        "multi-01-agent-trend": (90, 300),
        "timeout-01-kb-timeout": (60, 300),
        "timeout-02-web-timeout": (60, 300),
    }


def test_mock_keeps_the_offline_token_budget(settings: Settings) -> None:
    assert token_budget_for(settings, "mock") == settings.token_budget


def test_live_provider_gets_its_own_token_budget(settings: Settings) -> None:
    assert token_budget_for(settings, "openai") == settings.token_budget_live


def test_the_two_defaults_match_the_measured_live_spend() -> None:
    """Defaults: 80k for the offline regression, 160k for real models."""
    assert Settings.model_fields["token_budget"].default == 80_000
    assert Settings.model_fields["token_budget_live"].default == 160_000


def test_explicit_request_model_and_budget_override_shared_provider_defaults(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    live = settings.model_copy(
        update={
            "provider": "openai",
            "api_key": "test-key",
            "model": "server-default",
            "token_budget_live": 160_000,
        }
    )
    shared = OpenAICompatibleProvider(live)
    pipeline = ResearchPipeline(settings=live, provider=shared, knowledge_base=knowledge_base)
    request = ResearchRequest(
        question="verify request model settings",
        settings=ResearchSettings(
            model="request-model",
            temperature=0.0,
            presence_penalty=0.0,
            frequency_penalty=-0.5,
            max_tokens=321,
            token_budget=1_234,
        ),
    )

    effective = pipeline._request_settings(request)
    runtime = pipeline._build_runtime("task", request.question, effective, Tracer("task"), request)
    try:
        assert runtime.llm.provider.model_name() == "request-model"
        assert runtime.llm.temperature == 0.0
        assert runtime.llm.max_tokens == 321
        assert runtime.llm.max_tokens_override == 321
        assert runtime.llm.presence_penalty == 0.0
        assert runtime.llm.frequency_penalty == -0.5
        assert runtime.llm.token_budget == 1_234
    finally:
        runtime.tools.close()
        close = getattr(runtime.llm.provider, "close", None)
        if close is not None:
            close()
        shared.close()
