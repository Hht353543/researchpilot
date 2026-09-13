"""End-to-end pipeline integration: agents, tools, MCP, memory, tracing."""

from __future__ import annotations

from pathlib import Path

from researchpilot.agents.base import build_runtime
from researchpilot.agents.planner import PlannerAgent
from researchpilot.agents.researcher import ResearchAgent
from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.mcp.client import InProcessMcpClient
from researchpilot.mcp.server import McpServer
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest


def _pipeline(settings: Settings, kb: KnowledgeBase, **kwargs: object) -> ResearchPipeline:
    return ResearchPipeline(
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=kb,
        **kwargs,  # type: ignore[arg-type]
    )


def test_pipeline_produces_grounded_report(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    pipeline = _pipeline(settings, knowledge_base)
    result = pipeline.run(ResearchRequest(question="MCP 基于什么协议，核心方法有哪些？"))

    assert result.status in {"succeeded", "degraded"}
    assert result.plan is not None
    assert result.plan.subtasks
    assert result.evidence.evidence
    assert result.evidence.sources
    assert result.verification is not None
    assert result.verification.checks
    assert result.report is not None
    assert "JSON-RPC" in result.report.markdown
    # every citation in the markdown resolves to real evidence
    import re

    cited = set(re.findall(r"\[(E\d+)\]", result.report.markdown))
    assert cited
    assert cited <= {item.id for item in result.evidence.evidence}
    assert result.metrics.tool_calls >= 1
    assert result.metrics.llm_calls >= 4
    assert result.metrics.usage.total_tokens > 0
    assert result.metrics.latency_ms > 0


def test_pipeline_records_trace_with_agent_hierarchy(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    pipeline = _pipeline(settings, knowledge_base)
    result = pipeline.run(ResearchRequest(question="工具注册表需要声明哪些字段？"))
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None
    agent_spans = {span.name for span in trace.spans if span.kind == "agent"}
    assert {"PlannerAgent", "ResearchAgent", "VerifierAgent", "CriticAgent", "WriterAgent"} <= agent_spans
    assert any(span.kind == "llm" for span in trace.spans)
    assert any(span.kind == "tool" for span in trace.spans)
    assert any(span.kind == "retrieval" for span in trace.spans)
    assert trace.metrics.usage.total_tokens == result.metrics.usage.total_tokens


def test_pipeline_uses_mcp_tool(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    mcp_client = InProcessMcpClient(McpServer(settings, knowledge_base=knowledge_base))
    pipeline = _pipeline(settings, knowledge_base, mcp_client=mcp_client)
    result = pipeline.run(ResearchRequest(question="如何通过 MCP 网关把知识库复用到多个 Agent？"))
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None
    mcp_spans = [span for span in trace.spans if span.kind == "mcp"]
    assert mcp_spans, "expected the MCP tool to be called"
    assert result.metrics.mcp_calls >= 1


def test_pipeline_handles_tool_failure_gracefully(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    from researchpilot.evaluation.fault_injection import build_fault_tool

    def factory(registry: object) -> dict[str, object]:
        return {
            "knowledge_search": build_fault_tool(registry, {"tool": "knowledge_search", "mode": "failure"})
        }

    pipeline = _pipeline(settings, knowledge_base, tool_overrides=factory)
    result = pipeline.run(ResearchRequest(question="检索知识库说明混合检索的作用。"))
    assert result.status in {"degraded", "failed"}
    assert result.metrics.tool_failures >= 1
    assert result.errors
    # the pipeline still returns a typed result instead of raising
    assert result.report is not None


def test_pipeline_enforces_tool_budget(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    from researchpilot.tools.registry import ToolPolicy

    pipeline = _pipeline(settings, knowledge_base, tool_policy=ToolPolicy(max_calls_per_task=1))
    result = pipeline.run(ResearchRequest(question="分析记忆分层与上下文管理，并给出优缺点。"))
    assert result.metrics.tool_failures >= 1
    assert any("budget" in message for message in result.errors) or result.status == "degraded"


def test_async_submission_returns_task_id(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    import time

    pipeline = _pipeline(settings, knowledge_base)
    task_id = pipeline.submit(ResearchRequest(question="长期记忆需要哪些字段与 TTL 策略？"))
    assert task_id
    deadline = time.time() + 30
    result = None
    while time.time() < deadline:
        result = pipeline.result_store.get(task_id)
        if result is not None and result.status not in {"pending", "running"}:
            break
        time.sleep(0.2)
    assert result is not None
    assert result.status in {"succeeded", "degraded"}


def test_planner_falls_back_when_provider_fails(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    from typing import Any

    from pydantic import BaseModel

    from researchpilot.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse

    class BrokenProvider(LLMProvider):
        name = "broken"

        def model_name(self) -> str:
            return "broken"

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
            raise LLMError("provider down")

    pipeline = ResearchPipeline(settings=settings, provider=BrokenProvider(), knowledge_base=knowledge_base)
    result = pipeline.run(ResearchRequest(question="混合检索的作用是什么？"))
    assert result.status == "degraded"
    assert result.plan is not None
    assert result.plan.subtasks


def test_runtime_builds_default_toolset(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    runtime = build_runtime(task_id="t", question="q", settings=settings, knowledge_base=knowledge_base)
    names = set(runtime.tools.names())
    assert {"knowledge_search", "web_search", "document_reader", "calculator", "metadata"} <= names
    runtime.tools.close()


def test_agents_share_working_memory(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    runtime = build_runtime(
        task_id="t2", question="工具注册表字段", settings=settings, knowledge_base=knowledge_base
    )
    plan = PlannerAgent(runtime).run("工具注册表需要声明哪些字段？")
    bundle = ResearchAgent(runtime).run(plan)
    assert runtime.memory.working.evidence
    assert runtime.memory.working.subtasks == plan.subtasks
    assert len(bundle.evidence) == len(runtime.memory.working.evidence)
    runtime.tools.close()


# --------------------------------------------------------------------------- #
# Graceful degradation: the cases the audit requires to be handled explicitly
# --------------------------------------------------------------------------- #
def test_empty_knowledge_base_falls_back_to_web_and_reports_gaps(settings: Settings) -> None:
    """Empty KB must not abort the task: other sources are used and gaps recorded."""
    from researchpilot.rag.knowledge_base import KnowledgeBase as KB

    empty_kb = KB(settings)  # never ingested
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=empty_kb
    )
    result = pipeline.run(ResearchRequest(question="空知识库下混合检索的表现如何？"))
    assert result.status in {"degraded", "succeeded"}
    assert result.report is not None
    assert result.report.markdown.strip()
    if result.evidence.evidence:
        assert all(item.source_id.startswith("web:") for item in result.evidence.evidence), (
            "only non-KB sources can provide evidence when the KB is empty"
        )
    assert result.evidence.gaps


def test_no_sources_available_reports_explicit_gap(settings: Settings, tmp_path) -> None:
    """KB empty *and* web corpus missing -> explicit gap, never an invented answer."""
    from researchpilot.rag.knowledge_base import KnowledgeBase as KB

    barren = settings.model_copy(update={"web_corpus_path": str(tmp_path / "no-corpus")})
    pipeline = ResearchPipeline(settings=barren, provider=MockLLMProvider(barren), knowledge_base=KB(barren))
    result = pipeline.run(ResearchRequest(question="空知识库下混合检索的表现如何？"))
    assert result.status in {"degraded", "failed"}
    assert result.report is not None
    assert not result.evidence.evidence
    assert any(marker in result.report.markdown for marker in ("缺口", "未获得", "无可用证据", "不足"))


def test_empty_retrieval_is_reported_not_invented(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    from researchpilot.evaluation.fault_injection import build_fault_tool

    def factory(registry: object) -> dict[str, object]:
        return {"knowledge_search": build_fault_tool(registry, {"tool": "knowledge_search", "mode": "empty"})}

    pipeline = ResearchPipeline(
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
        tool_overrides=factory,
    )
    result = pipeline.run(ResearchRequest(question="检索知识库说明混合检索的作用。"))
    assert result.status in {"degraded", "failed", "succeeded"}
    assert result.report is not None
    assert not any(item.tool == "knowledge_search" for item in result.evidence.evidence), (
        "empty knowledge_search must not produce knowledge_search evidence"
    )
    markdown = result.report.markdown
    assert any(marker in markdown for marker in ("缺口", "未获得", "不足", "无可用证据"))


def test_embedding_failure_degrades_instead_of_crashing(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    class BrokenEmbedder:
        name = "broken"
        dimension = 64

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedding backend unavailable")

        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("embedding backend unavailable")

    broken_kb = KnowledgeBase(settings, store=knowledge_base.store, embedder=BrokenEmbedder())
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=broken_kb
    )
    result = pipeline.run(ResearchRequest(question="混合检索依赖什么？"))
    assert result.status in {"degraded", "failed"}
    assert any("embedding backend unavailable" in message for message in result.errors)
    assert result.report is not None


def test_vector_store_failure_degrades_instead_of_crashing(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    class BrokenStore:
        def __getattr__(self, item: str):
            raise RuntimeError("vector store unavailable")

    broken_kb = KnowledgeBase(settings, store=BrokenStore(), embedder=knowledge_base.embedder)
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=broken_kb
    )
    result = pipeline.run(ResearchRequest(question="向量库不可用时系统如何表现？"))
    assert result.status in {"degraded", "failed"}
    assert result.metrics.tool_failures >= 1
    assert any("vector store unavailable" in message for message in result.errors)


def test_llm_rerank_strategy_is_exercised(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    rerank_settings = settings.model_copy(update={"rerank_strategy": "llm"})
    pipeline = ResearchPipeline(
        settings=rerank_settings,
        provider=MockLLMProvider(rerank_settings),
        knowledge_base=knowledge_base,
    )
    result = pipeline.run(ResearchRequest(question="MCP 的核心方法有哪些？"))
    assert result.report is not None
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None
    rerank_spans = [span for span in trace.spans_of("llm") if "rerank" in span.name]
    assert rerank_spans, "llm rerank strategy must actually call the model"


def test_tool_cache_serves_repeated_queries(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    cached_settings = settings.model_copy(update={"tool_cache_ttl_s": 120.0})
    runtime = build_runtime(
        task_id="cache-task",
        question="混合检索",
        settings=cached_settings,
        provider=MockLLMProvider(cached_settings),
        knowledge_base=knowledge_base,
    )
    try:
        assert runtime.tools.cache_ttl_s == 120.0, "settings must reach the tool registry"
        ctx = runtime.tool_context
        first = runtime.tools.invoke("knowledge_search", {"query": "混合检索", "top_k": 2}, ctx)
        second = runtime.tools.invoke("knowledge_search", {"query": "混合检索", "top_k": 2}, ctx)
        assert first.ok and second.ok
        assert second.cached is True
        # A different query must NOT be served from the first entry's cache.
        third = runtime.tools.invoke("knowledge_search", {"query": "工具权限分级", "top_k": 2}, ctx)
        assert third.cached is False
    finally:
        runtime.tools.close()


def test_prompt_injection_evidence_is_quarantined(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    from researchpilot.rag.loader import DocumentLoader

    poisoned = DocumentLoader().load_path(
        Path(__file__).resolve().parents[2] / "data" / "samples" / "poisoned_document.md"
    )
    knowledge_base.ingest_documents(poisoned)
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=knowledge_base
    )
    result = pipeline.run(ResearchRequest(question="供应商文档中关于 Agent 网关默认权限的说明是什么？"))
    markdown = result.report.markdown if result.report else ""
    assert "已删除全部文档" not in markdown
    assert "rm -rf / 已执行" not in markdown
    assert result.verification is not None
    if result.verification.flagged_sources:
        assert any("注入" in item for item in result.report.limitations)


def test_mcp_failure_degrades_without_crashing(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """Spec §18.9: an MCP outage must be a recorded tool failure, not a crash."""
    from researchpilot.mcp.client import McpClientError

    class BrokenMcpClient:
        name = "broken"

        def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
            raise McpClientError("MCP server unreachable")

        def list_tools(self) -> list[dict[str, object]]:
            return []

    pipeline = ResearchPipeline(
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
        mcp_client=BrokenMcpClient(),
    )
    result = pipeline.run(ResearchRequest(question="如何通过 MCP 网关复用知识库？"))
    assert result.status in {"degraded", "succeeded"}
    assert result.metrics.tool_failures >= 1
    assert any("mcp" in message.lower() for message in result.errors)
    assert result.report is not None and result.report.markdown.strip()


def test_mcp_transport_falls_back_visibly(tmp_path) -> None:
    """An unreachable MCP transport falls back to in-process and reports so."""
    from researchpilot.api.service import ServiceContainer

    settings = Settings(
        provider="mock",
        kb_path="tests/fixtures/kb",
        runs_path=str(tmp_path / "runs"),
        embedding_dim=64,
        mcp_transport="http",
        mcp_url="http://127.0.0.1:9/mcp",  # nothing listens here
        mcp_timeout_s=1.0,
    )
    container = ServiceContainer(settings)
    assert container.mcp_mode == "inprocess-fallback"
    assert container.knowledge_base.stats()["documents"] >= 1
