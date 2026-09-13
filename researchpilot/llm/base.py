"""Provider-agnostic LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.schemas import TokenUsage


class LLMError(RuntimeError):
    """Base class for provider failures."""


class LLMConfigError(LLMError):
    """Provider misconfiguration (missing key, bad base url, ...)."""


class StructuredOutputError(LLMError):
    """The model did not produce output that validates against the schema."""


class BudgetExceededError(LLMError):
    """The task token budget was exhausted."""


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMResponse(BaseModel):
    text: str
    model: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str = "stop"
    attempts: int = 1
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMProvider(ABC):
    """Every provider (mock, OpenAI-compatible, local) implements this."""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> LLMResponse:
        """Return a completion. ``response_schema`` requests structured output."""

    @abstractmethod
    def model_name(self) -> str: ...

    def supports_json_schema(self) -> bool:
        return False
