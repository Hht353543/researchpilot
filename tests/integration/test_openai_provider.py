"""End-to-end verification of the real (HTTP) OpenAI-compatible provider path.

These tests talk to a scripted OpenAI-compatible endpoint over real HTTP, so they
exercise request payloads, bearer auth, structured-output parsing, retries,
timeouts, usage accounting, embeddings and the full agent pipeline - the same
code path that a real API key would use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMConfigError, LLMError
from researchpilot.llm.openai_provider import OpenAICompatibleProvider
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.embeddings import OpenAIEmbedder
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import CritiqueReport, ResearchRequest
from tests.fixtures.openai_stub import run_stub_server

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _settings(tmp_path: Path, base_url: str, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "test-key",
        "base_url": base_url,
        "kb_path": str(FIXTURES / "kb"),
        "runs_path": str(tmp_path / "runs"),
        "embedding_dim": 64,
        "top_k": 4,
        "retrieve_k": 8,
        "max_iterations": 1,
        "token_budget": 200_000,
        "tool_cache_ttl_s": 0.0,
    }
    data.update(overrides)
    return Settings(**data)  # type: ignore[arg-type]


def test_pipeline_end_to_end_over_http(tmp_path: Path) -> None:
    with run_stub_server() as (base_url, state):
        settings = _settings(tmp_path, base_url)
        knowledge_base = KnowledgeBase(settings)
        knowledge_base.ingest_path(FIXTURES / "kb")
        pipeline = ResearchPipeline(settings=settings, knowledge_base=knowledge_base)
        result = pipeline.run(ResearchRequest(question="MCP 基于什么协议，核心方法有哪些？"))

        assert result.status in {"succeeded", "degraded"}
        assert result.report is not None
        markdown = result.report.markdown
        assert markdown.strip()
        assert "[E" in markdown  # citations are bound to evidence ids
        assert "参考文献" in markdown

        cited = {e.id for e in result.evidence.evidence}
        assert cited, "the HTTP provider path must still produce evidence"
        assert "tools/list" not in markdown or cited  # sanity: grounded text only

        # usage reported by the endpoint is propagated into metrics and cost
        assert result.metrics.usage.total_tokens > 0
        assert result.metrics.usage.cost_usd > 0  # gpt-4o-mini is in the price table
        assert result.metrics.llm_calls >= 4

        trace = pipeline.trace_store.get(result.task_id)
        assert trace is not None
        llm_spans = trace.spans_of("llm")
        assert llm_spans
        assert all(span.model == "gpt-4o-mini" for span in llm_spans)
        assert any(span.usage.total_tokens > 0 for span in llm_spans)
        assert set(state.last_schemas) >= {
            "ResearchPlan",
            "EvidenceBundle",
            "VerificationReport",
            "CritiqueReport",
            "FinalReport",
        }
        assert state.chat_requests >= 4


def test_embeddings_provider_against_http_endpoint(tmp_path: Path) -> None:
    with run_stub_server() as (base_url, state):
        settings = _settings(tmp_path, base_url, embedding_provider="openai")
        embedder = OpenAIEmbedder(settings)
        vectors = embedder.embed(["混合检索 RRF", "工具注册表权限"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 64
        assert vectors[0] != vectors[1]
        assert state.embedding_requests == 1

        knowledge_base = KnowledgeBase(settings, embedder=embedder)
        report = knowledge_base.ingest_path(FIXTURES / "kb")
        assert report.chunks > 0
        hits = knowledge_base.search("工具注册表需要声明哪些字段", top_k=3)
        assert hits.hits
        assert state.embedding_requests >= 2


def test_provider_retries_on_server_error(tmp_path: Path) -> None:
    with run_stub_server(fail_first=1) as (base_url, state):
        provider = OpenAICompatibleProvider(_settings(tmp_path, base_url))
        response = provider.complete(
            [ChatMessage(role="user", content="hi")],
            response_schema=CritiqueReport,
            purpose="critic",
        )
        assert response.attempts == 2
        assert state.chat_requests == 2
        assert CritiqueReport.model_validate_json(response.text).overall_assessment


def test_provider_surfaces_missing_api_key() -> None:
    with pytest.raises(LLMConfigError):
        OpenAICompatibleProvider(Settings(provider="openai", api_key=None))


def test_provider_maps_401_to_config_error(tmp_path: Path) -> None:
    with run_stub_server(api_key="server-key") as (base_url, _state):
        provider = OpenAICompatibleProvider(_settings(tmp_path, base_url, api_key="wrong-key"))
        with pytest.raises(LLMConfigError):
            provider.complete([ChatMessage(role="user", content="hi")])


def test_provider_enforces_timeout(tmp_path: Path) -> None:
    with run_stub_server(latency_s=0.6) as (base_url, _state):
        provider = OpenAICompatibleProvider(
            _settings(tmp_path, base_url, request_timeout_s=0.1, max_retries=0)
        )
        with pytest.raises(LLMError):
            provider.complete([ChatMessage(role="user", content="slow")])


def test_pipeline_reports_config_error_without_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "http://127.0.0.1:9/v1", api_key=None)
    pipeline = ResearchPipeline(settings=settings)
    result = pipeline.run(ResearchRequest(question="工具注册表需要哪些字段？"))
    assert result.status == "failed"
    assert any("llm_config" in message for message in result.errors)
