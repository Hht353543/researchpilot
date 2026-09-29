"""calculator: safe AST-based arithmetic (no eval, no attribute access)."""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from researchpilot.tools.base import (
    BaseTool,
    ToolContext,
    ToolItem,
    ToolPermission,
    ToolRequestContext,
    ToolResult,
)

_BIN_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
}
_MAX_EXPRESSION = 300
_EXPRESSION_RE = re.compile(r"[0-9][0-9\s.+\-*/()%^]{2,}[0-9%]")


class CalculatorArgs(BaseModel):
    expression: str = Field(
        min_length=1, max_length=_MAX_EXPRESSION, description="arithmetic expression, e.g. (13-8)/8*100"
    )
    precision: int = Field(default=4, ge=0, le=10)


class CalculatorTool(BaseTool):
    name = "calculator"
    description = (
        "Evaluate an arithmetic expression safely (+, -, *, /, //, %, **, sqrt/log/exp/abs/"
        "round/min/max/sum). Use it for growth rates, ratios and cost estimates."
    )
    permission = ToolPermission.COMPUTE
    timeout_s = 3.0
    max_retries = 0
    tags = ["compute"]
    args_model = CalculatorArgs

    def build_arguments(self, request: ToolRequestContext) -> dict[str, Any] | None:
        """Extract an arithmetic expression from the sub-question, if there is one."""
        match = _EXPRESSION_RE.search(request.question)
        if match is None:
            return None
        expression = match.group(0).replace("×", "*").replace("÷", "/")
        return {"expression": expression}

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CalculatorArgs)
        try:
            value = _evaluate(args.expression)
        except Exception as exc:
            return ToolResult(tool=self.name, ok=False, error=f"invalid expression: {exc}")
        if isinstance(value, int | float) and not math.isfinite(float(value)):
            return ToolResult(tool=self.name, ok=False, error="result is not finite")
        rounded = round(float(value), args.precision)
        content = f"计算：{args.expression} = {rounded}"
        return ToolResult(
            tool=self.name,
            ok=True,
            items=[
                ToolItem(
                    source_id=f"calc:{abs(hash(args.expression)) % 10**8:08d}",
                    content=content,
                    title="calculator",
                    kind="computation",
                    metadata={"expression": args.expression, "result": rounded},
                )
            ],
            data={"expression": args.expression, "result": rounded, "precision": args.precision},
        )


def _evaluate(expression: str) -> Any:
    if len(expression) > _MAX_EXPRESSION:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree.body)


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ValueError("only numeric literals are allowed")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"operator not allowed: {type(node.op).__name__}")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS.get(type(node.op))
        if unary is None:
            raise ValueError(f"unary operator not allowed: {type(node.op).__name__}")
        return unary(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ValueError("only whitelisted functions are allowed")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        return _FUNCTIONS[node.func.id](*[_eval_node(arg) for arg in node.args])
    if isinstance(node, ast.Tuple | ast.List):
        return [_eval_node(element) for element in node.elts]
    raise ValueError(f"expression element not allowed: {type(node).__name__}")
