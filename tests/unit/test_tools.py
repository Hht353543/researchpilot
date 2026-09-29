"""Tool registry contract: schema validation, permissions, timeout, retry, budget."""

from __future__ import annotations

import threading
import time

import pytest
from pydantic import BaseModel

from researchpilot.lifecycle import LifecycleCancelled, TaskLifecycle
from researchpilot.observability.trace import Tracer
from researchpilot.tools.base import BaseTool, ToolContext, ToolPermission, ToolResult
from researchpilot.tools.implementations.calculator import CalculatorTool
from researchpilot.tools.registry import ToolPolicy, ToolRegistry


class EchoArgs(BaseModel):
    text: str


class EchoTool(BaseTool):
    name = "echo"
    description = "echo back"
    permission = ToolPermission.READ_ONLY
    args_model = EchoArgs

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EchoArgs)
        return ToolResult(tool=self.name, data={"text": args.text})


class SlowTool(BaseTool):
    name = "slow"
    description = "sleeps"
    permission = ToolPermission.COMPUTE
    timeout_s = 0.2
    max_retries = 1
    args_model = EchoArgs

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        time.sleep(0.6)
        return ToolResult(tool=self.name)


class FlakyTool(BaseTool):
    name = "flaky"
    description = "fails first, then succeeds"
    permission = ToolPermission.COMPUTE
    max_retries = 2
    args_model = EchoArgs

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult(tool=self.name, ok=False, error="first call fails")
        return ToolResult(tool=self.name, data={"attempt": self.calls})


class BlockingTool(BaseTool):
    name = "blocking"
    description = "waits for a test event"
    permission = ToolPermission.COMPUTE
    timeout_s = 5
    max_retries = 0
    args_model = EchoArgs

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        return ToolResult(tool=self.name)


def test_registry_lists_specs_with_schemas() -> None:
    registry = ToolRegistry([EchoTool(), CalculatorTool()])
    specs = {spec["name"]: spec for spec in registry.specs()}
    assert set(specs) == {"echo", "calculator"}
    assert specs["echo"]["input_schema"]["properties"]["text"]
    assert specs["calculator"]["permission"] == "compute"
    assert specs["calculator"]["timeout_s"] > 0
    assert all("description" in entry for entry in registry.catalogue())


def test_registry_invokes_and_validates_arguments() -> None:
    registry = ToolRegistry([EchoTool()])
    ok = registry.invoke("echo", {"text": "hi"})
    assert ok.ok and ok.data["text"] == "hi"
    bad = registry.invoke("echo", {"wrong": 1})
    assert not bad.ok and "invalid arguments" in (bad.error or "")
    missing = registry.invoke("nope", {})
    assert not missing.ok and "unknown tool" in (missing.error or "")


def test_registry_enforces_permission_policy() -> None:
    registry = ToolRegistry([EchoTool(), CalculatorTool()], policy=ToolPolicy(denied_tools={"echo"}))
    denied = registry.invoke("echo", {"text": "x"})
    assert not denied.ok and "denied" in (denied.error or "")

    restricted = ToolRegistry(
        [CalculatorTool()], policy=ToolPolicy(allowed_permissions={ToolPermission.READ_ONLY})
    )
    blocked = restricted.invoke("calculator", {"expression": "1+1"})
    assert not blocked.ok and "permission" in (blocked.error or "")


def test_registry_times_out_and_retries() -> None:
    tracer = Tracer("t-timeout", "q")
    registry = ToolRegistry([SlowTool()])
    started = time.perf_counter()
    result = registry.invoke("slow", {"text": "x"}, ToolContext(task_id="t", tracer=tracer))
    elapsed = time.perf_counter() - started
    assert not result.ok
    assert "timeout" in (result.error or "")
    assert elapsed < 2.0
    assert any(span.kind == "retry" for span in tracer.spans)


def test_registry_observes_cancellation_while_blocking_tool_unwinds() -> None:
    tool = BlockingTool()
    registry = ToolRegistry([tool])
    lifecycle = TaskLifecycle(timeout_s=10)
    assert lifecycle.transition("running")
    ctx = ToolContext(task_id="cancel-tool", lifecycle=lifecycle)
    observed: list[type[BaseException]] = []

    def invoke() -> None:
        try:
            registry.invoke("blocking", {"text": "x"}, ctx)
        except LifecycleCancelled as exc:
            observed.append(type(exc))

    worker = threading.Thread(target=invoke)
    worker.start()
    assert tool.entered.wait(timeout=2)
    assert lifecycle.cancel()
    worker.join(timeout=1)
    assert observed == [LifecycleCancelled]
    assert tool.calls == 1
    with pytest.raises(LifecycleCancelled):
        registry.invoke("blocking", {"text": "again"}, ctx)
    tool.release.set()
    registry.close()


def test_registry_retries_until_success() -> None:
    tool = FlakyTool()
    registry = ToolRegistry([tool])
    result = registry.invoke("flaky", {"text": "x"})
    assert result.ok
    assert result.attempts == 2


def test_registry_enforces_call_budget() -> None:
    registry = ToolRegistry([EchoTool()], policy=ToolPolicy(max_calls_per_task=2))
    ctx = ToolContext(task_id="budget-task")
    assert registry.invoke("echo", {"text": "1"}, ctx).ok
    assert registry.invoke("echo", {"text": "2"}, ctx).ok
    third = registry.invoke("echo", {"text": "3"}, ctx)
    assert not third.ok and "budget" in (third.error or "")


def test_registry_caching() -> None:
    registry = ToolRegistry([FlakyTool()], cache_ttl_s=30)
    first = registry.invoke("flaky", {"text": "x"})
    second = registry.invoke("flaky", {"text": "x"})
    assert first.ok and second.ok
    assert second.cached
    tool = registry.get("flaky")
    assert isinstance(tool, FlakyTool), "the registry must return the registered tool instance"
    assert tool.calls == 2  # 1 failure + 1 success, then cache hit


def test_registry_membership_helpers() -> None:
    registry = ToolRegistry([EchoTool()])
    assert registry.has("echo")
    assert not registry.has("nope")
    names = registry.names()
    registry.unregister("echo")
    assert registry.names() != names
