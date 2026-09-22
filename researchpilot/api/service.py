"""Service container: one place that wires knowledge base, tools, MCP and pipeline."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress
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
from researchpilot.task_store import TaskStore

logger = logging.getLogger("researchpilot.service")


class ServiceContainer:
    """Holds process-wide singletons (KB index, stores, MCP client, pipeline)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self.knowledge_base = KnowledgeBase.load_or_create(self.settings)
        self.trace_store = TraceStore(self.settings.runs_dir())
        self.result_store = ResultStore(self.settings.runs_dir())
        self.task_store = TaskStore(self.settings.runs_dir())
        self.provider: LLMProvider = build_provider(self.settings)
        self.mcp_server = McpServer(self.settings, knowledge_base=self.knowledge_base)
        self.mcp_mode = "inprocess"
        self.mcp_client = self._build_mcp_client()
        self.pipeline = ResearchPipeline(
            settings=self.settings,
            provider=self.provider,
            knowledge_base=self.knowledge_base,
            trace_store=self.trace_store,
            result_store=self.result_store,
            task_store=self.task_store,
            mcp_client=self.mcp_client,
        )
        self._closed = False

    def _build_mcp_client(self) -> Any:
        """Connect to the configured MCP transport, falling back to in-process.

        The API and the MCP server are separate services in docker-compose; a short
        retry loop covers container start-up ordering, and ``self.mcp_mode`` records
        which transport is really in use so /health cannot mislead operators.
        """
        try:
            if self.settings.mcp_transport == "inprocess":
                return InProcessMcpClient(self.mcp_server)
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    client = build_mcp_client(self.settings)
                    self.mcp_mode = getattr(client, "name", self.settings.mcp_transport)
                    return client
                except Exception as exc:  # pragma: no cover - startup race
                    last_error = exc
                    time.sleep(0.5 * (attempt + 1))
            raise last_error if last_error else RuntimeError("mcp client unavailable")
        except Exception as exc:
            logger.warning(
                "MCP transport %s unavailable (%s); falling back to in-process",
                self.settings.mcp_transport,
                exc,
            )
            self.mcp_mode = "inprocess-fallback"
            return InProcessMcpClient(self.mcp_server)

    # -- task execution ---------------------------------------------------- #
    def run_research(self, request: ResearchRequest) -> Any:
        return self.pipeline.run(request)

    def submit_research(self, request: ResearchRequest) -> str:
        return self.pipeline.submit(request)

    def cancel_research(self, task_id: str) -> bool:
        return self.pipeline.cancel(task_id)

    def close(self) -> None:
        """Stop task admission, signal workers, then close application-owned clients."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.pipeline.request_shutdown()
        close_mcp = getattr(self.mcp_client, "close", None)
        if callable(close_mcp):
            with suppress(Exception):
                close_mcp()
        close_provider = getattr(self.provider, "close", None)
        if callable(close_provider):
            with suppress(Exception):
                close_provider()
        self.pipeline.close()
        with suppress(Exception):
            self.knowledge_base.close()

    # -- knowledge base ---------------------------------------------------- #
    def ingest_document(
        self,
        content: str,
        *,
        title: str,
        source: str,
        metadata: dict[str, Any],
    ) -> Any:
        with self._lock:
            return self.knowledge_base.ingest_text_and_save(
                content,
                title=title,
                source=source,
                metadata=metadata,
            )

    def reingest(self) -> Any:
        with self._lock:
            return self.knowledge_base.reindex_and_save()

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            return self.knowledge_base.delete_document(doc_id)

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
            cache_ttl_s=self.settings.tool_cache_ttl_s,
        )
        names = registry.names()
        registry.close()
        return names
