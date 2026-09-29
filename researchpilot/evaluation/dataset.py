"""Golden dataset schema + loader."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

TaskCategory = Literal[
    "simple_fact",
    "multi_step",
    "rag",
    "tool_calling",
    "mcp",
    "multi_agent",
    "citation",
    "prompt_injection",
    "tool_abuse",
    "timeout_recovery",
    "no_result",
    "bad_source",
]

DEFAULT_DATASET = Path("eval") / "golden_dataset.jsonl"


class Expectations(BaseModel):
    """Machine-checkable acceptance criteria for one task."""

    require_report: bool = True
    required_any: list[list[str]] = Field(
        default_factory=list,
        description="OR-groups; every group must be satisfied by the report markdown",
    )
    forbidden: list[str] = Field(default_factory=list)
    min_citation_integrity: float = 0.0
    min_retrieval_recall: float = 0.0
    min_context_relevance: float = 0.0
    require_gap_statement: bool = False
    min_tool_failures: int = 0
    max_tool_failures: int | None = None
    status_in: list[str] = Field(default_factory=list)
    max_latency_s: float | None = None
    # A real model cannot meet an offline regression budget (the fastest task in the
    # 2026-09-15 deepseek run took 61.1s), so live runs read a separate budget and
    # the offline gate keeps its original number.
    max_latency_s_live: float | None = None


class GoldenTask(BaseModel):
    id: str
    category: TaskCategory
    question: str
    expected_docs: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    expectations: Expectations = Field(default_factory=Expectations)
    setup: dict[str, Any] = Field(
        default_factory=dict,
        description="optional fault injection / tool overrides for this task",
    )
    notes: str = ""


def load_dataset(path: str | Path | None = None) -> list[GoldenTask]:
    file = Path(path) if path is not None else DEFAULT_DATASET
    if not file.exists():
        raise FileNotFoundError(f"golden dataset not found: {file}")
    tasks: list[GoldenTask] = []
    if file.suffix == ".jsonl":
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                tasks.append(GoldenTask.model_validate(json.loads(line)))
    else:
        payload = json.loads(file.read_text(encoding="utf-8"))
        items = payload.get("tasks", payload) if isinstance(payload, dict) else payload
        tasks = [GoldenTask.model_validate(item) for item in items]
    seen: set[str] = set()
    for task in tasks:
        if task.id in seen:
            raise ValueError(f"duplicate task id in golden dataset: {task.id}")
        seen.add(task.id)
    return tasks


def dataset_stats(tasks: list[GoldenTask]) -> dict[str, Any]:
    by_category = Counter(task.category for task in tasks)
    return {
        "tasks": len(tasks),
        "categories": dict(sorted(by_category.items())),
        "with_expected_docs": sum(1 for t in tasks if t.expected_docs),
        "with_expected_tools": sum(1 for t in tasks if t.expected_tools),
        "fault_injection_tasks": sum(1 for t in tasks if "fault" in t.setup),
    }
