"""A not-yet-configured desktop can serve its setup UI without a fake research model."""

from __future__ import annotations

from typing import Any

from researchpilot.llm.base import ChatMessage, LLMConfigError, LLMProvider, LLMResponse


class UnconfiguredProvider(LLMProvider):
    name = "unconfigured"

    def __init__(self, model: str) -> None:
        self.model = model

    def model_name(self) -> str:
        return self.model

    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> LLMResponse:
        raise LLMConfigError("请在设置中输入 API Key，测试并保存后开始研究。")
