"""Fault-injection tool wrappers used by resilience evaluation tasks."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel

from researchpilot.tools.base import BaseTool, ToolContext, ToolRequestContext, ToolResult


class DelegatingTool(BaseTool):
    """Base class for wrappers that forward to the tool registered under ``inner``."""

    def __init__(self, inner_name: str, *, registry: Any = None, **kwargs: Any) -> None:
        self.inner_name = inner_name
        self.registry = registry
        inner = registry.get(inner_name) if registry is not None else None
        self._inner_tool: BaseTool | None = inner
        self.name = inner_name
        self.args_model = inner.args_model if inner is not None else BaseModel
        self.description = (inner.description if inner else "") + f" [{self.fault_label}]"
        self.permission = inner.permission if inner else self.permission
        self.span_kind = inner.span_kind if inner else "tool"
        self.tags = list(inner.tags) if inner else []
        for key, value in kwargs.items():
            setattr(self, key, value)

    fault_label = "delegate"

    def _inner(self) -> BaseTool:
        if self._inner_tool is None:
            raise RuntimeError(f"DelegatingTool needs an existing tool named {self.inner_name!r}")
        return self._inner_tool

    def build_arguments(self, request: ToolRequestContext) -> dict[str, Any] | None:
        """Fault wrappers keep the real tool's argument policy, so the fault is
        actually exercised (otherwise the wrapper would be skipped entirely)."""
        return self._inner().build_arguments(request)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return self._inner().run(args, ctx)


class SlowTool(DelegatingTool):
    """Sleeps before delegating so the registry timeout can be exercised."""

    fault_label = "slow"
    delay_s = 3.0
    timeout_s = 1.0
    max_retries = 1

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        time.sleep(self.delay_s)
        return self._inner().run(args, ctx)


class FailingTool(DelegatingTool):
    """Always fails, simulating an external API outage."""

    fault_label = "failing"
    max_retries = 0

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            tool=self.name,
            ok=False,
            error="injected failure: upstream search API returned HTTP 503",
        )


class EmptyTool(DelegatingTool):
    """Returns no items, simulating a query with no matching documents."""

    fault_label = "empty"
    max_retries = 0

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(tool=self.name, ok=True, items=[], data={"injected": "empty result set"})


def build_fault_tool(registry: Any, spec: dict[str, Any]) -> DelegatingTool:
    """``{"tool": "knowledge_search", "mode": "timeout", ...}`` -> wrapper tool."""
    mode = str(spec.get("mode", "timeout"))
    tool_name = str(spec.get("tool", "knowledge_search"))
    if mode == "timeout":
        return SlowTool(
            tool_name,
            registry=registry,
            delay_s=float(spec.get("delay_s", 3.0)),
            timeout_s=float(spec.get("timeout_s", 1.0)),
            max_retries=int(spec.get("max_retries", 1)),
        )
    if mode == "failure":
        return FailingTool(tool_name, registry=registry)
    if mode == "empty":
        return EmptyTool(tool_name, registry=registry)
    raise ValueError(f"unknown fault mode: {mode}")
