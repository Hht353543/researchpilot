"""Service container: one place that wires knowledge base, tools, MCP and pipeline."""

from __future__ import annotations

import threading
from typing import Any

from researchpilot.agents.base import ResearchRuntime, build_runtime
from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import LLMProvider
from researchpilot.llm.factory import build_provider
from researchpilot.mcp.client import InProcessMcpClient, build_mcp_client
from researchpilot.mcp.server import McpServer
from researchpilot.observability.trace import TraceStore
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest
from researchpilot.store import ResultStore


class ServiceContainer:
    """Holds process-wide singletons (KB index, stores, MCP client, pipeline)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self.knowledge_base = KnowledgeBase.load_or_create(self.settings)
        self.trace_store = TraceStore(self.settings.runs_dir())
        self.result_store = ResultStore(self.settings.runs_dir())
        self.provider: LLMProvider = build_provider(self.settings)
        self.mcp_server = McpServer(self.settings, knowledge_base=self.knowledge_base)
        self.mcp_client = self._build_mcp_client()
        self.pipeline = ResearchPipeline(
            settings=self.settings,
            provider=self.provider,
            knowledge_base=self.knowledge_base,
            trace_store=self.trace_store,
            result_store=self.result_store,
            mcp_client=self.mcp_client,
        )

    def _build_mcp_client(self) -> Any:
        try:
            if self.settings.mcp_transport == "inprocess":
                return InProcessMcpClient(self.mcp_server)
            return build_mcp_client(self.settings)
        except Exception:
            return InProcessMcpClient(self.mcp_server)

    # -- task execution ---------------------------------------------------- #
    def run_research(self, request: ResearchRequest) -> Any:
        return self.pipeline.run(request)

    def submit_research(self, request: ResearchRequest) -> str:
        return self.pipeline.submit(request)

    # -- knowledge base ---------------------------------------------------- #
    def reingest(self) -> Any:
        with self._lock:
            report = self.knowledge_base.load_default()
            self.knowledge_base.save()
            return report

    def runtime(self, task_id: str, question: str) -> ResearchRuntime:
        return build_runtime(
            task_id=task_id,
            question=question,
            settings=self.settings,
            provider=self.provider,
            knowledge_base=self.knowledge_base,
            mcp_client=self.mcp_client,
        )

    def pipeline_tool_names(self) -> list[str]:
        from researchpilot.tools.registry import build_default_registry
        from researchpilot.tools.web_backend import build_web_backend

        registry = build_default_registry(
            settings=self.settings,
            services={
                "knowledge_base": self.knowledge_base,
                "web_backend": build_web_backend(self.settings),
                "mcp_client": self.mcp_client,
            },
        )
        names = registry.names()
        registry.close()
        return names
