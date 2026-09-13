"""OpenAI-compatible provider (works with OpenAI, DeepSeek, vLLM, Ollama, ...)."""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel

from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMConfigError, LLMError, LLMProvider, LLMResponse
from researchpilot.schemas import TokenUsage
from researchpilot.utils import estimate_tokens


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.api_key:
            raise LLMConfigError(
                "API_KEY is not set. Use RESEARCHPILOT_PROVIDER=mock for offline runs, or export "
                "API_KEY / BASE_URL / MODEL for a real provider."
            )
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.request_timeout_s,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    def model_name(self) -> str:
        return self.settings.model

    def supports_json_schema(self) -> bool:
        return True

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
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [m.as_dict() for m in messages],
            "temperature": (self.settings.temperature if temperature is None else float(temperature)),
            "presence_penalty": (
                self.settings.presence_penalty if presence_penalty is None else float(presence_penalty)
            ),
            "frequency_penalty": (
                self.settings.frequency_penalty if frequency_penalty is None else float(frequency_penalty)
            ),
            "max_tokens": self.settings.max_tokens if max_tokens is None else int(max_tokens),
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": _strict_schema(response_schema),
                },
            }

        last_error: str = ""
        for attempt in range(self.settings.max_retries + 1):
            if attempt:
                time.sleep(min(2**attempt * 0.25, 4.0))
            try:
                response = self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                continue
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                continue

            if response.status_code >= 400:
                body = response.text[:400]
                last_error = f"HTTP {response.status_code}: {body}"
                if response.status_code in {401, 403, 404}:
                    raise LLMConfigError(last_error)
                if response.status_code == 400 and "response_format" in body:
                    payload.pop("response_format", None)
                    continue
                if response.status_code < 500:
                    raise LLMError(last_error)
                continue

            data: dict[str, Any] = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            if not text and message.get("reasoning_content"):
                text = message["reasoning_content"]
            usage_raw = data.get("usage") or {}
            prompt_tokens = int(usage_raw.get("prompt_tokens") or estimate_tokens(_join(messages)))
            completion_tokens = int(usage_raw.get("completion_tokens") or estimate_tokens(text))
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=int(usage_raw.get("total_tokens") or prompt_tokens + completion_tokens),
            )
            return LLMResponse(
                text=text,
                model=str(data.get("model") or self.settings.model),
                usage=usage,
                finish_reason=str(choice.get("finish_reason") or "stop"),
                attempts=attempt + 1,
                raw={"id": data.get("id", ""), "purpose": purpose},
            )
        raise LLMError(f"provider openai failed after retries: {last_error}")

    def close(self) -> None:
        self._client.close()


def _join(messages: list[ChatMessage]) -> str:
    return "\n".join(m.content for m in messages)


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Best-effort strict JSON schema for structured outputs."""
    return schema.model_json_schema()
