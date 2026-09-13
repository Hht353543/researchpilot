"""Shared agent runtime: tracers, tools, memory, sources and the LLM runner."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import LLMProvider
from researchpilot.llm.factory import build_structured_runner
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.memory.manager import MemoryManager
from researchpilot.observability.trace import Tracer
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.sources import SourceRegistry
from researchpilot.tools.base import ToolContext
from researchpilot.tools.registry import ToolRegistry, build_default_registry
from researchpilot.tools.web_backend import build_web_backend


@dataclass
class ResearchRuntime:
    """Everything an agent needs, injected once per task."""

    task_id: str
    tracer: Tracer
    settings: Settings
    llm: StructuredLLMRunner
    tools: ToolRegistry
    knowledge_base: KnowledgeBase
    memory: MemoryManager
    sources: SourceRegistry = field(default_factory=SourceRegistry)
    web_backend: Any = None
    mcp_client: Any = None
    scratch: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def tool_context(self) -> ToolContext:
        return ToolContext(
            task_id=self.task_id,
            tracer=self.tracer,
            settings=self.settings,
            services={
                "knowledge_base": self.knowledge_base,
                "web_backend": self.web_backend,
                "llm_runner": self.llm,
                "mcp_client": self.mcp_client,
                "registry": self.tools,
            },
            scratch=dict(self.scratch),
        )


def build_runtime(
    *,
    task_id: str,
    question: str,
    settings: Settings | None = None,
    tracer: Tracer | None = None,
    provider: LLMProvider | None = None,
    knowledge_base: KnowledgeBase | None = None,
    tools: ToolRegistry | None = None,
    memory: MemoryManager | None = None,
    mcp_client: Any = None,
    sources: SourceRegistry | None = None,
    policy: Any = None,
) -> ResearchRuntime:
    settings = settings or get_settings()
    tracer = tracer or Tracer(task_id, question)
    knowledge_base = knowledge_base or KnowledgeBase.load_or_create(settings)
    web_backend = build_web_backend(settings)
    llm = build_structured_runner(settings, provider=provider, tracer=tracer)
    if tools is None:
        tools = build_default_registry(
            settings=settings,
            services={
                "knowledge_base": knowledge_base,
                "web_backend": web_backend,
                "mcp_client": mcp_client,
            },
            policy=policy,
            cache_ttl_s=settings.tool_cache_ttl_s,
        )
    return ResearchRuntime(
        task_id=task_id,
        tracer=tracer,
        settings=settings,
        llm=llm,
        tools=tools,
        knowledge_base=knowledge_base,
        memory=memory or MemoryManager(task_id=task_id),
        sources=sources or SourceRegistry(),
        web_backend=web_backend,
        mcp_client=mcp_client,
    )


class BaseAgent:
    """Common behaviour: named spans, memory notes and error capture."""

    name: str = "Agent"

    def __init__(self, runtime: ResearchRuntime) -> None:
        self.runtime = runtime

    @contextlib.contextmanager
    def span(self, *, input: dict[str, Any] | None = None) -> Iterator[Any]:
        with self.runtime.tracer.span(self.name, "agent", agent=self.name, input=input or {}) as span:
            yield span

    def note(self, content: str) -> None:
        self.runtime.memory.note("agent", content, agent=self.name)
