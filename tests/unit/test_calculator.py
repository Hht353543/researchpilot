"""Calculator tool must be safe (AST whitelist) and useful."""

from __future__ import annotations

from researchpilot.tools.base import ToolContext, ToolResult
from researchpilot.tools.implementations.calculator import CalculatorTool


def _run(expression: str) -> ToolResult:
    tool = CalculatorTool()
    return tool.run(tool.validate({"expression": expression}), ToolContext())


def test_simple_arithmetic() -> None:
    result = _run("61/48 * 100")
    assert result.ok
    assert abs(result.data["result"] - 127.0833) < 0.01
    assert "127.08" in result.items[0].content


def test_whitelisted_functions() -> None:
    assert _run("sqrt(16)").data["result"] == 4.0
    assert _run("max(1, 2, 3)").data["result"] == 3.0


def test_rejects_code_execution() -> None:
    for expression in [
        "__import__('os').system('echo hi')",
        "open('/etc/passwd').read()",
        "lambda: 1",
        "1 if True else 2",
    ]:
        assert not _run(expression).ok, expression


def test_rejects_non_numeric_literals() -> None:
    assert not _run("'abc' * 3").ok
