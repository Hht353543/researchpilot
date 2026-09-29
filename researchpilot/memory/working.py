"""Working memory: the structured state of the current research task."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from researchpilot.schemas import Evidence, ResearchPlan, Subtask
from researchpilot.utils import jaccard, sha1_of


class WorkingMemory(BaseModel):
    """Plan + sub-tasks + evidence + intermediate observations (de-duplicated)."""

    task_id: str = ""
    objective: str = ""
    plan: ResearchPlan | None = None
    subtasks: list[Subtask] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    iterations: int = 0
    notes: list[str] = Field(default_factory=list)
    scratch: dict[str, Any] = Field(default_factory=dict)

    def set_plan(self, plan: ResearchPlan) -> None:
        self.plan = plan
        self.objective = plan.objective
        self.subtasks = list(plan.subtasks)

    def add_evidence(self, items: list[Evidence]) -> list[Evidence]:
        keys = {_evidence_key(existing) for existing in self.evidence}
        added: list[Evidence] = []
        for item in items:
            key = _evidence_key(item)
            if key in keys:
                continue
            if any(jaccard(existing.claim, item.claim) >= 0.8 for existing in self.evidence) or any(
                jaccard(added_item.claim, item.claim) >= 0.8 for added_item in added
            ):
                continue
            keys.add(key)
            self.evidence.append(item)
            added.append(item)
        return added

    def add_observation(self, tool: str, arguments: dict[str, Any], summary: dict[str, Any]) -> None:
        self.observations.append({"tool": tool, "arguments": arguments, "summary": summary})

    def uncovered_subtasks(self) -> list[Subtask]:
        covered = {e.subtask_id for e in self.evidence}
        return [s for s in self.subtasks if s.id not in covered and s.intent != "synthesis"]

    def evidence_for(self, subtask_id: str) -> list[Evidence]:
        return [e for e in self.evidence if e.subtask_id == subtask_id]

    def token_estimate(self) -> int:
        from researchpilot.utils import estimate_tokens

        return estimate_tokens(self.snapshot_json())

    def snapshot_json(self) -> str:
        return self.model_dump_json()

    def to_prompt(self, *, limit: int = 40) -> str:
        lines = [f"objective: {self.objective}", f"iterations: {self.iterations}"]
        for subtask in self.subtasks[:12]:
            lines.append(f"- [{subtask.id}] {subtask.question} (tools={subtask.tools})")
        for evidence in self.evidence[:limit]:
            lines.append(f"  * [{evidence.id}] {evidence.claim} <- {evidence.source_id}")
        return "\n".join(lines)


def _evidence_key(evidence: Evidence) -> str:
    return sha1_of(f"{evidence.source_id}|{evidence.claim[:120]}")
