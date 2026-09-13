"""End-to-end pipeline integration: agents, tools, MCP, memory, tracing."""

from __future__ import annotations

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
