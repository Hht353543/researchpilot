"""Memory facade: short-term + working + long-term in one object."""

from __future__ import annotations

from typing import Any

from researchpilot.memory.long_term import LongTermMemory
from researchpilot.memory.models import MemoryKind, MemoryRecord
from researchpilot.memory.short_term import ShortTermMemory
from researchpilot.memory.working import WorkingMemory
from researchpilot.schemas import Evidence, ResearchPlan


class MemoryManager:
    def __init__(
        self,
        *,
        task_id: str = "",
        long_term: LongTermMemory | None = None,
        short_term: ShortTermMemory | None = None,
        working: WorkingMemory | None = None,
    ) -> None:
        self.short_term = short_term or ShortTermMemory()
        self.working = working or WorkingMemory(task_id=task_id)
        self.long_term = long_term if long_term is not None else LongTermMemory()

    # -- short term -------------------------------------------------------- #
    def note(self, role: str, content: str, *, agent: str = "") -> None:
        self.short_term.add(role, content, agent=agent)  # type: ignore[arg-type]

    # -- working ----------------------------------------------------------- #
    def set_plan(self, plan: ResearchPlan) -> None:
        self.working.set_plan(plan)

    def add_evidence(self, items: list[Evidence]) -> list[Evidence]:
        return self.working.add_evidence(items)

    def add_observation(self, tool: str, arguments: dict[str, Any], summary: dict[str, Any]) -> None:
        self.working.add_observation(tool, arguments, summary)

    # -- long term --------------------------------------------------------- #
    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind = "fact",
        importance: float = 0.5,
        source: str = "",
        ttl_days: float | None = None,
    ) -> MemoryRecord:
        return self.long_term.remember(
            content, kind=kind, importance=importance, source=source, ttl_days=ttl_days
        )

    def recall_context(self, query: str, *, limit: int = 5) -> str:
        records = self.long_term.recall(query, limit=limit)
        if not records:
            return ""
        return "\n".join(f"- ({r.kind}) {r.content}" for r in records)

    def persist_task_result(self, *, objective: str, conclusions: list[tuple[str, float]]) -> int:
        """Store the task topic plus high-confidence conclusions in long-term memory."""
        stored = 0
        self.remember(f"研究主题：{objective}", kind="topic", importance=0.4, source="task", ttl_days=180)
        stored += 1
        for statement, confidence in conclusions:
            if confidence < 0.5:
                continue
            self.remember(
                statement,
                kind="conclusion",
                importance=min(0.5 + confidence * 0.4, 0.95),
                source="report",
                ttl_days=120,
            )
            stored += 1
        return stored

    def snapshot(self) -> dict[str, Any]:
        return {
            "short_term_tokens": self.short_term.total_tokens(),
            "short_term_items": len(self.short_term.items),
            "working_evidence": len(self.working.evidence),
            "working_subtasks": len(self.working.subtasks),
            "long_term_records": len(self.long_term.all()),
        }
