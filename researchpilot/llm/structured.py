"""Structured-output runtime: schema validation, repair retries, token budgeting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from researchpilot.llm.base import (
    BudgetExceededError,
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    StructuredOutputError,
)
from researchpilot.observability.trace import Tracer
from researchpilot.schemas import TokenUsage
from researchpilot.utils import estimate_tokens, extract_json_block

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass
class StructuredCallResult(Generic[ModelT]):
    """Result of one structured call, typed by the requested schema."""

    value: ModelT
    response: LLMResponse
    usage: TokenUsage
    attempts: int
    errors: list[str] = field(default_factory=list)


@dataclass
class TextCallResult:
    text: str
    response: LLMResponse
    usage: TokenUsage
    attempts: int


class StructuredLLMRunner:
    """Wraps a provider with validation + bounded repair retries + tracing."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        tracer: Tracer | None = None,
        max_repair_retries: int = 2,
        token_budget: int = 80_000,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> None:
        self.provider = provider
        self.tracer = tracer
        self.max_repair_retries = max_repair_retries
        self.token_budget = token_budget
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.usage = TokenUsage()

    # -- budgets ----------------------------------------------------------- #
    def _charge(self, usage: TokenUsage) -> None:
        self.usage = self.usage.add(usage)
        if self.token_budget > 0 and self.usage.total_tokens > self.token_budget:
            raise BudgetExceededError(
                f"token budget exceeded: {self.usage.total_tokens} > {self.token_budget}"
            )

    def remaining_budget(self) -> int:
        if self.token_budget <= 0:
            return 10**9
        return max(self.token_budget - self.usage.total_tokens, 0)

    # -- calls ------------------------------------------------------------- #
    def run(
        self,
        schema: type[ModelT],
        *,
        agent: str,
        system: str,
        user: str,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> StructuredCallResult[ModelT]:
        purpose = purpose or schema.__name__
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        errors: list[str] = []
        attempts = 0
        total = TokenUsage()

        for attempt in range(self.max_repair_retries + 1):
            attempts += 1
            span = None
            if self.tracer is not None:
                span = self.tracer.start_span(
                    f"{agent}.llm[{purpose}]",
                    "llm",
                    agent=agent,
                    model=self.provider.model_name(),
                    input={"messages": [m.as_dict() for m in messages], "attempt": attempts},
                )
            try:
                response = self.provider.complete(
                    messages,
                    response_schema=schema,
                    hints=hints,
                    purpose=purpose,
                    temperature=self.temperature if temperature is None else temperature,
                    max_tokens=self.max_tokens if max_tokens is None else max_tokens,
                )
            except LLMError:
                if span is not None and self.tracer is not None:
                    self.tracer.end_span(span, error="provider failure")
                raise
            except Exception as exc:  # pragma: no cover - provider specific
                if span is not None and self.tracer is not None:
                    self.tracer.end_span(span, error=f"{type(exc).__name__}: {exc}")
                raise LLMError(f"provider {self.provider.name} failed: {exc}") from exc

            total = total.add(response.usage)
            self._charge(response.usage)

            if span is not None and self.tracer is not None:
                self.tracer.end_span(
                    span,
                    output={"text": response.text[:4000], "finish_reason": response.finish_reason},
                    usage=response.usage,
                    error=None,
                    metadata={"attempt": attempts},
                )

            try:
                payload = extract_json_block(response.text)
                value = schema.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                errors.append(f"attempt {attempts}: {type(exc).__name__}: {exc}")
                if attempt >= self.max_repair_retries:
                    raise StructuredOutputError(
                        f"{schema.__name__} validation failed after {attempts} attempts: {errors[-1]}"
                    ) from exc
                if self.tracer is not None:
                    with self.tracer.span(
                        f"{agent}.retry[{purpose}]",
                        "retry",
                        agent=agent,
                        input={"error": errors[-1][:500]},
                    ):
                        pass
                messages = [
                    messages[0],
                    messages[1],
                    ChatMessage(role="assistant", content=response.text[:4000]),
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous answer was rejected: "
                            f"{errors[-1][:600]}\nReturn ONLY a corrected JSON object that matches "
                            f"{schema.__name__}."
                        ),
                    ),
                ]
                continue
            return StructuredCallResult(
                value=value, response=response, usage=total, attempts=attempts, errors=errors
            )

        raise StructuredOutputError(  # pragma: no cover - loop always returns/raises
            f"{schema.__name__} failed after {attempts} attempts"
        )

    def run_text(
        self,
        *,
        agent: str,
        system: str,
        user: str,
        purpose: str = "text",
        hints: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextCallResult:
        messages = [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]
        span = None
        if self.tracer is not None:
            span = self.tracer.start_span(
                f"{agent}.llm[{purpose}]",
                "llm",
                agent=agent,
                model=self.provider.model_name(),
                input={"messages": [m.as_dict() for m in messages]},
            )
        try:
            response = self.provider.complete(
                messages,
                response_schema=None,
                hints=hints,
                purpose=purpose,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            )
        except Exception as exc:
            if span is not None and self.tracer is not None:
                self.tracer.end_span(span, error=f"{type(exc).__name__}: {exc}")
            raise LLMError(f"provider {self.provider.name} failed: {exc}") from exc
        self._charge(response.usage)
        if span is not None and self.tracer is not None:
            self.tracer.end_span(span, output={"text": response.text[:2000]}, usage=response.usage)
        return TextCallResult(text=response.text.strip(), response=response, usage=response.usage, attempts=1)


def estimated_usage(prompt: str, completion: str) -> TokenUsage:
    """Utility used by mock/offline providers and estimators."""
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(completion)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=0.0,
    )
