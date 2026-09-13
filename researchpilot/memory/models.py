"""Memory record model shared by the three memory layers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.utils import parse_iso

MemoryKind = Literal["preference", "topic", "conclusion", "fact", "task"]


class MemoryRecord(BaseModel):
    id: str
    content: str
    kind: MemoryKind = "fact"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str = ""
    created_at: str
    expires_at: str | None = None
    hits: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_expired(self, *, now: float) -> bool:
        if not self.expires_at:
            return False
        stamp = parse_iso(self.expires_at)
        return stamp is not None and stamp <= now
