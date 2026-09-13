"""Shared fixtures: isolated settings, knowledge base and API client."""

from __future__ import annotations

from pathlib import Path

import pytest

from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.rag.knowledge_base import KnowledgeBase

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio only (trio is not a dependency)."""
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="mock",
        kb_path=str(FIXTURES / "kb"),
        runs_path=str(tmp_path / "runs"),
        embedding_dim=128,
        top_k=4,
        retrieve_k=8,
        max_iterations=2,
        token_budget=200_000,
        web_search_mode="offline",
        mcp_transport="inprocess",
    )


@pytest.fixture
def knowledge_base(settings: Settings) -> KnowledgeBase:
    kb = KnowledgeBase(settings)
    kb.ingest_path(FIXTURES / "kb")
    kb.save()
    return kb


@pytest.fixture
def provider() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
def runner(provider: MockLLMProvider) -> StructuredLLMRunner:
    return StructuredLLMRunner(provider, token_budget=200_000)
