"""Two spec-mandated resilience paths that need end-to-end (not unit) evidence:

* §12/§13: a tool timeout must surface through the real HTTP API as a degraded
  result with recorded failures - not a hang and not a 500;
* §18.6: LLM output validation - a provider that returns unparseable text for
  every call must degrade into the deterministic fallbacks instead of producing
  a fabricated report.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from researchpilot.api.app import create_app
from researchpilot.config import Settings
from researchpilot.evaluation.fault_injection import build_fault_tool
from researchpilot.llm.base import ChatMessage, LLMProvider, LLMResponse
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest, TokenUsage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class AlwaysGarbageProvider(LLMProvider):
    """Returns text that is never valid JSON (worst-case LLM output)."""

    name = "garbage"

    def model_name(self) -> str:
        return "garbage-model"

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
        return LLMResponse(
            text="I am sorry, I cannot produce JSON today. (no braces at all)",
            model=self.model_name(),
            usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )


@pytest.mark.anyio
async def test_tool_timeout_surfaces_through_the_api(tmp_path) -> None:
    """A slow tool must not hang or 500 the research endpoint."""
    settings = Settings(
        provider="mock",
        kb_path=str(FIXTURES / "kb"),
        runs_path=str(tmp_path / "runs"),
        embedding_dim=64,
        top_k=3,
        max_iterations=1,
        request_timeout_s=5,
    )
    app = create_app(settings)
    container = app.state.container
    container.pipeline.tool_overrides = lambda registry: {
        "knowledge_search": build_fault_tool(
            registry,
            {
                "tool": "knowledge_search",
                "mode": "timeout",
                "delay_s": 1.5,
                "timeout_s": 0.2,
                "max_retries": 0,
            },
        )
    }

    transport = httpx.ASGITransport(app=app)
    started = time.perf_counter()
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.post(
            "/research", json={"question": "请检索知识库说明混合检索的作用。"}, timeout=60
        )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in {"degraded", "failed"}
    assert payload["metrics"]["tool_failures"] >= 1
    assert any("timeout" in message.lower() for message in payload["errors"]), payload["errors"]
    assert payload["report"] is not None
    # The run must be bounded by the tool timeout budget, not by the slow tool.
    assert elapsed < 60


def test_unparseable_llm_output_degrades_without_fabricating(settings: Settings, tmp_path) -> None:
    """§18.6: invalid LLM output must fall back, never invent a report."""
    knowledge_base = KnowledgeBase(settings)
    knowledge_base.ingest_path(FIXTURES / "kb")
    pipeline = ResearchPipeline(
        settings=settings,
        provider=AlwaysGarbageProvider(),
        knowledge_base=knowledge_base,
    )
    result = pipeline.run(ResearchRequest(question="工具注册表需要声明哪些字段？"))

    assert result.status in {"degraded", "failed"}
    assert result.errors, "garbage output must be recorded as an error"
    # Deterministic fallbacks still produce a structured plan and a report.
    assert result.plan is not None and result.plan.subtasks
    assert result.report is not None
    markdown = result.report.markdown
    assert markdown.strip()
    # Local retrieval is untouched by the broken model, so evidence is extractive.
    for item in result.evidence.evidence:
        assert item.quote, "extractive fallback must carry verbatim quotes"
        assert item.source_id
    # No conclusion may be claimed without evidence.
    for claim in result.report.conclusions:
        assert claim.evidence_ids, f"uncited conclusion survived: {claim.statement[:40]}"
