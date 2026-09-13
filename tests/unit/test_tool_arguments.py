"""Tool-owned argument policy: no if/elif tool dispatch inside the agents."""

from __future__ import annotations

from researchpilot.tools.base import ToolRequestContext
from researchpilot.tools.implementations.calculator import CalculatorTool
from researchpilot.tools.implementations.document_reader import DocumentReaderTool
from researchpilot.tools.implementations.knowledge_search import KnowledgeSearchTool
from researchpilot.tools.implementations.mcp_tool import McpResearchContextTool
from researchpilot.tools.implementations.metadata import MetadataTool
from researchpilot.tools.implementations.web_search import WebSearchTool


def test_knowledge_search_builds_hybrid_query() -> None:
    args = KnowledgeSearchTool().build_arguments(ToolRequestContext(question="混合检索如何工作", top_k=5))
    assert args is not None
    assert args["query"] == "混合检索如何工作"
    assert args["strategy"] == "hybrid"
    assert args["top_k"] == 5
    assert args["rewrite"] is True


def test_web_and_mcp_build_queries() -> None:
    web = WebSearchTool().build_arguments(ToolRequestContext(question="market trend", top_k=6))
    assert web == {"query": "market trend", "top_k": 5}

    mcp = McpResearchContextTool().build_arguments(
        ToolRequestContext(question="知识库网关", subtask_id="S3", top_k=6)
    )
    assert mcp == {"query": "知识库网关", "subtask": "S3", "top_k": 5}


def test_document_reader_requires_a_document_hint() -> None:
    tool = DocumentReaderTool()
    assert tool.build_arguments(ToolRequestContext(question="读取文档")) is None
    args = tool.build_arguments(ToolRequestContext(question="读取文档", doc_id_hint="doc-1"))
    assert args == {"doc_id": "doc-1", "max_chars": 6000}


def test_calculator_extracts_expressions_only_when_present() -> None:
    tool = CalculatorTool()
    assert tool.build_arguments(ToolRequestContext(question="请解释 RAG 的原理")) is None
    args = tool.build_arguments(ToolRequestContext(question="请计算 61/48 * 100 的增长率"))
    assert args is not None
    assert args["expression"].startswith("61/48")
    assert tool.validate(args).expression


def test_metadata_defaults_to_statistics() -> None:
    args = MetadataTool().build_arguments(ToolRequestContext(question="覆盖度如何"))
    assert args == {"subject": "stats", "limit": 20}


def test_fault_wrapper_delegates_argument_policy() -> None:
    from researchpilot.evaluation.fault_injection import build_fault_tool
    from researchpilot.tools.registry import ToolRegistry

    registry = ToolRegistry([KnowledgeSearchTool()])
    wrapper = build_fault_tool(registry, {"tool": "knowledge_search", "mode": "failure"})
    args = wrapper.build_arguments(ToolRequestContext(question="q", top_k=3))
    assert args is not None and args["query"] == "q"


def test_every_registered_tool_implements_argument_policy() -> None:
    """Registry-wide contract: adding a tool never requires editing the agents."""
    from researchpilot.config import Settings
    from researchpilot.tools.registry import build_default_registry

    registry = build_default_registry(
        settings=Settings(provider="mock", embedding_dim=32, web_search_mode="offline"),
        services={"knowledge_base": None, "web_backend": None, "mcp_client": object()},
    )
    try:
        for name in registry.names():
            tool = registry.get(name)
            assert hasattr(tool, "build_arguments")
            spec = tool.spec()
            assert spec.input_schema.get("type") == "object"
            assert spec.permission
            assert spec.timeout_s > 0
    finally:
        registry.close()
