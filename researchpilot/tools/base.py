"""Tool contract: schema, permission, timeout, retry - all declarative."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.lifecycle import TaskLifecycle
from researchpilot.observability.trace import Tracer
from researchpilot.schemas import SourceKind, SpanKind


class ToolPermission(StrEnum):
    READ_ONLY = "read_only"
    NETWORK = "network"
    COMPUTE = "compute"
    WRITE = "write"


class ToolItem(BaseModel):
    """Normalised unit returned by retrieval-like tools."""

    source_id: str
    content: str
    title: str = ""
    kind: SourceKind = "knowledge_base"
    doc_id: str = ""
    chunk_id: str = ""
    url: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "content": self.content,
            "score": round(self.score, 4),
            "url": self.url,
            "doc_id": self.doc_id,
            "section": self.metadata.get("section", ""),
        }


class ToolResult(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    items: list[ToolItem] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    cached: bool = False

    def rendered(self) -> str:
        if self.items:
            return "\n\n".join(f"[{item.source_id}] {item.title}\n{item.content}" for item in self.items)
        return json.dumps(self.data or {"ok": self.ok, "error": self.error}, ensure_ascii=False)

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "items": [item.as_prompt_dict() for item in self.items],
            "data": self.data,
            "error": self.error,
        }


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permission: ToolPermission = ToolPermission.READ_ONLY
    timeout_s: float = 10.0
    max_retries: int = 1
    tags: list[str] = Field(default_factory=list)


@dataclass
class ToolContext:
    """Per-call context: tracing, config and shared services."""

    task_id: str = "adhoc"
    tracer: Tracer | None = None
    settings: Settings = field(default_factory=get_settings)
    services: dict[str, Any] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    lifecycle: TaskLifecycle | None = None

    def checkpoint(self) -> None:
        if self.lifecycle is not None:
            self.lifecycle.checkpoint()

    def remaining(self) -> float | None:
        return self.lifecycle.remaining() if self.lifecycle is not None else None

    def service(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def require(self, name: str) -> Any:
        value = self.services.get(name)
        if value is None:
            raise RuntimeError(f"tool context is missing required service '{name}'")
        return value


@dataclass
class ToolRequestContext:
    """What an agent knows when it decides how to call a tool for one sub-task.

    Tools own the policy that turns a research sub-task into their own typed
    arguments, so agents never need ``if tool_name == ...`` dispatch chains.
    """

    question: str
    subtask_id: str = ""
    agent: str = ""
    top_k: int = 6
    doc_id_hint: str = ""
    settings: Settings | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """Subclasses declare ``name``/``description``/``args_model`` and implement ``run``."""

    name: str = ""
    description: str = ""
    permission: ToolPermission = ToolPermission.READ_ONLY
    timeout_s: float = 10.0
    max_retries: int = 1
    tags: list[str] = []
    span_kind: SpanKind = "tool"
    args_model: type[BaseModel] = BaseModel
    output_model: type[BaseModel] | None = None

    @abstractmethod
    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...

    def build_arguments(self, request: ToolRequestContext) -> dict[str, Any] | None:
        """Default auto-invocation policy for this tool.

        Returning ``None`` means "this tool needs information the agent does not
        have; skip it for this sub-task" (e.g. ``document_reader`` without a doc id).
        """
        return None

    def validate(self, arguments: dict[str, Any]) -> BaseModel:
        return self.args_model.model_validate(arguments or {})

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.args_model.model_json_schema(),
            output_schema=(
                self.output_model.model_json_schema()
                if self.output_model is not None
                else ToolResult.model_json_schema()
            ),
            permission=self.permission,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            tags=list(self.tags),
        )

    def catalogue_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.value,
            "input_schema": self.args_model.model_json_schema().get("properties", {}),
        }
