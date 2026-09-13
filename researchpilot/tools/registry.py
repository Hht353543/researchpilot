"""Tool registry: routing, permissions, timeout, retry and tracing in one place."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from pydantic import ValidationError

from researchpilot.config import Settings, get_settings
from researchpilot.security import detect_injection
from researchpilot.tools.base import BaseTool, ToolContext, ToolPermission, ToolResult


class ToolError(RuntimeError):
    """Base class for tool failures surfaced to callers."""


class ToolNotFoundError(ToolError):
    """The requested tool is not registered."""


class ToolPermissionError(ToolError):
    """The tool (or its permission class) is blocked by policy."""


class ToolTimeoutError(ToolError):
    """The tool exceeded its declared timeout."""


class ToolBudgetError(ToolError):
    """The per-task tool-call budget is exhausted."""


class ToolPolicy:
    """What this registry is allowed to do (tool-abuse defence)."""

    def __init__(
        self,
        *,
        allowed_tools: set[str] | None = None,
        denied_tools: set[str] | None = None,
        allowed_permissions: set[ToolPermission] | None = None,
        max_calls_per_task: int = 40,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.denied_tools = denied_tools or set()
        self.allowed_permissions = allowed_permissions or {
            ToolPermission.READ_ONLY,
            ToolPermission.NETWORK,
            ToolPermission.COMPUTE,
        }
        self.max_calls_per_task = max_calls_per_task

    def check(self, tool: BaseTool) -> None:
        if self.allowed_tools is not None and tool.name not in self.allowed_tools:
            raise ToolPermissionError(f"tool '{tool.name}' is not in the allow-list")
        if tool.name in self.denied_tools:
            raise ToolPermissionError(f"tool '{tool.name}' is denied by policy")
        if tool.permission not in self.allowed_permissions:
            raise ToolPermissionError(f"tool '{tool.name}' requires permission '{tool.permission.value}'")


class ToolRegistry:
    """Single entry point for every tool call in the system."""

    def __init__(
        self,
        tools: list[BaseTool] | None = None,
        *,
        settings: Settings | None = None,
        policy: ToolPolicy | None = None,
        cache_ttl_s: float = 0.0,
        max_workers: int = 4,
    ) -> None:
        self.settings = settings or get_settings()
        self.policy = policy or ToolPolicy()
        self.cache_ttl_s = cache_ttl_s
        self._tools: dict[str, BaseTool] = {}
        self._cache: dict[str, tuple[float, ToolResult]] = {}
        self._calls: dict[str, int] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rp-tool")
        for tool in tools or []:
            self.register(tool)

    # -- registration ------------------------------------------------------ #
    def register(self, tool: BaseTool) -> None:
        if not tool.name:
            raise ValueError("tool must define a name")
        with self._lock:
            self._tools[tool.name] = tool

    def unregister(self, tool_name: str) -> None:
        with self._lock:
            self._tools.pop(tool_name, None)

    def get(self, tool_name: str) -> BaseTool:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {tool_name}")
        return tool

    def has(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def catalogue(self) -> list[dict[str, Any]]:
        return [self._tools[tool_name].catalogue_entry() for tool_name in self.names()]

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[tool_name].spec().model_dump() for tool_name in self.names()]

    def reset_call_counters(self) -> None:
        with self._lock:
            self._calls.clear()

    # -- invocation -------------------------------------------------------- #
    def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        ctx: ToolContext | None = None,
        *,
        raise_on_error: bool = False,
    ) -> ToolResult:
        arguments = dict(arguments or {})
        ctx = ctx or ToolContext(settings=self.settings)
        task_id = ctx.task_id

        try:
            tool = self.get(tool_name)
            self.policy.check(tool)
            self._account(task_id, tool_name)
            args = tool.validate(arguments)
        except (ToolNotFoundError, ToolPermissionError, ToolBudgetError) as exc:
            return self._failure(tool_name, arguments, str(exc), ctx, raise_on_error)
        except ValidationError as exc:
            return self._failure(
                tool_name, arguments, f"invalid arguments: {exc.errors()[:3]}", ctx, raise_on_error
            )

        cached = self._cache_get(tool_name, arguments)
        if cached is not None:
            return cached.model_copy(update={"cached": True})

        attempts = 0
        last_error: str | None = None
        while attempts <= tool.max_retries:
            attempts += 1
            span = None
            if ctx.tracer is not None:
                span = ctx.tracer.start_span(
                    f"tool.{tool_name}",
                    tool.span_kind,
                    agent=str(ctx.scratch.get("agent", "")),
                    tool=tool_name,
                    input={"arguments": _safe(arguments), "attempt": attempts},
                )
            started = time.perf_counter()
            try:
                result = self._run_with_timeout(tool, args, ctx)
                result = result.model_copy(
                    update={
                        "tool": tool_name,
                        "arguments": _safe(arguments),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "attempts": attempts,
                    }
                )
                if span is not None and ctx.tracer is not None:
                    ctx.tracer.end_span(
                        span,
                        output={
                            "ok": result.ok,
                            "items": len(result.items),
                            "data_keys": sorted(result.data),
                        },
                        error=result.error,
                        metadata={"attempts": attempts, "cached": result.cached},
                    )
                if result.ok:
                    self._cache_put(tool_name, arguments, result)
                    return result
                last_error = result.error or "tool returned an error"
            except ToolTimeoutError:
                last_error = f"timeout after {tool.timeout_s}s"
                if span is not None and ctx.tracer is not None:
                    ctx.tracer.end_span(span, error=last_error)
                if attempts <= tool.max_retries and ctx.tracer is not None:
                    with ctx.tracer.span(f"retry.tool.{tool_name}", "retry", tool=tool_name):
                        pass
            except Exception as exc:  # tool bug
                last_error = f"{type(exc).__name__}: {exc}"
                if span is not None and ctx.tracer is not None:
                    ctx.tracer.end_span(span, error=last_error)
            if attempts <= tool.max_retries:
                time.sleep(min(0.05 * attempts, 0.5))

        return self._failure(tool_name, arguments, last_error or "tool failed", ctx, raise_on_error)

    def _run_with_timeout(self, tool: BaseTool, args: Any, ctx: ToolContext) -> ToolResult:
        future: Future[ToolResult] = self._pool.submit(tool.run, args, ctx)
        try:
            return future.result(timeout=tool.timeout_s)
        except FutureTimeout as exc:
            future.cancel()
            raise ToolTimeoutError(f"tool '{tool.name}' timed out after {tool.timeout_s}s") from exc

    def _account(self, task_id: str, tool_name: str) -> None:
        with self._lock:
            count = self._calls.get(task_id, 0) + 1
            if count > self.policy.max_calls_per_task:
                raise ToolBudgetError(
                    f"tool-call budget exceeded for task {task_id} "
                    f"({count} > {self.policy.max_calls_per_task})"
                )
            self._calls[task_id] = count

    def _failure(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        message: str,
        ctx: ToolContext,
        raise_on_error: bool,
    ) -> ToolResult:
        if raise_on_error:
            raise ToolError(message)
        safe_args = _safe(arguments)
        result = ToolResult(
            tool=tool_name,
            arguments=safe_args,
            ok=False,
            error=message,
            data={"injection_signals": detect_injection(json.dumps(safe_args, ensure_ascii=False))},
        )
        if ctx.tracer is not None:
            span = ctx.tracer.start_span(
                f"tool.{tool_name}.error", "tool", tool=tool_name, input={"arguments": safe_args}
            )
            ctx.tracer.end_span(span, output={"ok": False}, error=message)
        return result

    # -- cache ------------------------------------------------------------- #
    def _cache_key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(_safe(arguments), sort_keys=True, ensure_ascii=False)}"

    def _cache_get(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult | None:
        if self.cache_ttl_s <= 0:
            return None
        key = self._cache_key(tool_name, arguments)
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, result = entry
        if time.time() - stored_at > self.cache_ttl_s:
            self._cache.pop(key, None)
            return None
        return result

    def _cache_put(self, tool_name: str, arguments: dict[str, Any], result: ToolResult) -> None:
        if self.cache_ttl_s <= 0:
            return
        self._cache[self._cache_key(tool_name, arguments)] = (time.time(), result)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _safe(arguments: dict[str, Any], *, limit: int = 600) -> dict[str, Any]:
    """Truncate large argument values before they land in a trace."""
    safe: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        raw = (
            value
            if isinstance(value, str | int | float | bool | type(None))
            else json.dumps(value, ensure_ascii=False)
        )
        rendered = str(raw)
        safe[key] = rendered[:limit] + ("…" if len(rendered) > limit else "")
    return safe


def build_default_registry(
    *,
    settings: Settings | None = None,
    services: dict[str, Any] | None = None,
    policy: ToolPolicy | None = None,
    cache_ttl_s: float = 0.0,
) -> ToolRegistry:
    """Wire the standard toolset (no if/elif dispatch anywhere)."""
    from researchpilot.tools.implementations.calculator import CalculatorTool
    from researchpilot.tools.implementations.document_reader import DocumentReaderTool
    from researchpilot.tools.implementations.knowledge_search import KnowledgeSearchTool
    from researchpilot.tools.implementations.mcp_tool import McpResearchContextTool
    from researchpilot.tools.implementations.metadata import MetadataTool
    from researchpilot.tools.implementations.web_search import WebSearchTool

    settings = settings or get_settings()
    services = services or {}
    registry = ToolRegistry(settings=settings, policy=policy, cache_ttl_s=cache_ttl_s)
    for tool_cls in (
        KnowledgeSearchTool,
        WebSearchTool,
        DocumentReaderTool,
        CalculatorTool,
        MetadataTool,
        McpResearchContextTool,
    ):
        tool = tool_cls()
        if isinstance(tool, McpResearchContextTool) and not services.get("mcp_client"):
            continue
        registry.register(tool)
    return registry
