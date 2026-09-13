"""Short-term memory: the current task's bounded context window."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.utils import estimate_tokens, normalize_text


class ShortTermItem(BaseModel):
    role: Literal["system", "user", "agent", "tool", "note"]
    content: str
    agent: str = ""
    tokens: int = 0


class ShortTermMemory(BaseModel):
    """Rolling window with a hard token budget (context-overflow defence)."""

    max_tokens: int = 4000
    max_items: int = 60
    items: list[ShortTermItem] = Field(default_factory=list)

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.items = list(self.items)[-self.max_items :]

    def add(
        self,
        role: Literal["system", "user", "agent", "tool", "note"],
        content: str,
        *,
        agent: str = "",
    ) -> ShortTermItem:
        item = ShortTermItem(role=role, content=content.strip(), agent=agent, tokens=estimate_tokens(content))
        self.items.append(item)
        if len(self.items) > self.max_items:
            self.items = self.items[-self.max_items :]
        self._trim()
        return item

    def _trim(self) -> None:
        while self.total_tokens() > self.max_tokens and len(self.items) > 1:
            self.items.pop(0)

    def total_tokens(self) -> int:
        return sum(item.tokens for item in self.items)

    def recent(self, limit: int = 10) -> list[ShortTermItem]:
        return self.items[-limit:]

    def to_prompt(self, limit: int = 10) -> str:
        return "\n".join(f"{item.role}: {item.content}" for item in self.recent(limit))

    def contains(self, text: str) -> bool:
        needle = normalize_text(text)
        return any(needle in normalize_text(item.content) for item in self.items)

    def clear(self) -> None:
        self.items.clear()
