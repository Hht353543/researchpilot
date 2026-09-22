"""Central configuration. Everything is env-var driven (12-factor)."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["mock", "openai"]
EmbeddingProviderName = Literal["hash", "openai"]
McpTransport = Literal["inprocess", "stdio", "http"]
WebSearchMode = Literal["offline", "http"]


class Settings(BaseSettings):
    """Runtime settings.

    Prefixed with ``RESEARCHPILOT_`` but the LLM knobs also accept the bare
    ``MODEL`` / ``API_KEY`` / ``BASE_URL`` / ... variables so the project drops
    into existing agent-environment conventions.
    """

    model_config = SettingsConfigDict(
        env_prefix="RESEARCHPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- LLM -----------------------------------------------------------------
    provider: ProviderName = "mock"
    model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("RESEARCHPILOT_MODEL", "MODEL", "model"),
    )
    api_key: str | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("RESEARCHPILOT_API_KEY", "API_KEY", "OPENAI_API_KEY", "api_key"),
    )
    base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("RESEARCHPILOT_BASE_URL", "BASE_URL", "base_url"),
    )
    temperature: float = Field(
        default=0.2, validation_alias=AliasChoices("RESEARCHPILOT_TEMPERATURE", "TEMPERATURE")
    )
    presence_penalty: float = Field(
        default=0.0,
        validation_alias=AliasChoices("RESEARCHPILOT_PRESENCE_PENALTY", "PRESENCE_PENALTY"),
    )
    frequency_penalty: float = Field(
        default=0.0,
        validation_alias=AliasChoices("RESEARCHPILOT_FREQUENCY_PENALTY", "FREQUENCY_PENALTY"),
    )
    max_tokens: int = Field(
        default=1200, validation_alias=AliasChoices("RESEARCHPILOT_MAX_TOKENS", "MAX_TOKENS")
    )
    request_timeout_s: float = 60.0
    max_retries: int = 2
    token_budget: int = 80_000
    # Real models spend far more per task than the offline mock (58.9k average,
    # 79.7k worst case in the 2026-09-15 deepseek run), so live runs get their own
    # ceiling. The offline budget keeps its value, which is what CI regresses on.
    token_budget_live: int = 160_000

    # --- Embeddings ----------------------------------------------------------
    embedding_provider: EmbeddingProviderName = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384

    # --- Retrieval -----------------------------------------------------------
    top_k: int = 6
    retrieve_k: int = 12
    max_iterations: int = 2
    rerank_strategy: Literal["heuristic", "llm"] = "heuristic"
    tool_cache_ttl_s: float = 60.0

    # --- Paths ---------------------------------------------------------------
    kb_path: str = "data/knowledge_base"
    runs_path: str = "runs"

    # --- MCP -----------------------------------------------------------------
    mcp_transport: McpTransport = "inprocess"
    mcp_url: str = "http://127.0.0.1:8765/mcp"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    mcp_timeout_s: float = 20.0

    # --- Web search ----------------------------------------------------------
    web_search_mode: WebSearchMode = "offline"
    web_corpus_path: str = "data/web_corpus"
    web_search_url: str = ""
    web_search_timeout_s: float = 10.0
    web_search_allowed_hosts: str = ""

    # --- Misc ----------------------------------------------------------------
    bind_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)
    allow_remote_access: bool = False
    access_token: str | None = Field(default=None, repr=False)
    max_request_body_bytes: int = Field(default=1_048_576, ge=128, le=16_777_216)
    max_concurrent_tasks: int = Field(default=4, ge=1, le=64)
    research_task_timeout_s: float = Field(default=300.0, gt=0, le=86_400)
    shutdown_timeout_s: float = Field(default=5.0, gt=0, le=60)
    task_heartbeat_interval_s: float = Field(default=5.0, gt=0, le=60)
    recovery_stale_after_s: float = Field(default=30.0, gt=0, le=3_600)
    log_level: str = "INFO"
    max_context_chars: int = 12_000
    pricing_file: str = "configs/pricing.yaml"

    @field_validator("access_token", mode="before")
    @classmethod
    def _normalise_access_token(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_runtime_boundary(self) -> Settings:
        self.validate_bind_host(self.bind_host)
        if self.recovery_stale_after_s <= self.task_heartbeat_interval_s:
            raise ValueError("recovery_stale_after_s must exceed task_heartbeat_interval_s")
        return self

    def validate_bind_host(self, host: str) -> None:
        """Reject accidental public binding unless shared mode is explicit and authenticated."""
        if _is_loopback_host(host):
            return
        if not self.allow_remote_access:
            raise ValueError("non-loopback binding requires RESEARCHPILOT_ALLOW_REMOTE_ACCESS=true")
        if not self.access_token:
            raise ValueError("remote access requires RESEARCHPILOT_ACCESS_TOKEN")
        if len(self.access_token) < 16:
            raise ValueError("RESEARCHPILOT_ACCESS_TOKEN must contain at least 16 characters")

    def kb_dir(self) -> Path:
        from researchpilot.utils import resolve_input_path

        return resolve_input_path(self.kb_path)

    def runs_dir(self) -> Path:
        return Path(self.runs_path)

    def price_table(self) -> dict[str, dict[str, float]]:
        """USD per 1M tokens, keyed by model name (``default`` is a fallback)."""
        table: dict[str, dict[str, float]] = {
            "default": {"input": 0.0, "output": 0.0},
            "mock": {"input": 0.0, "output": 0.0},
            "local": {"input": 0.0, "output": 0.0},
            "gpt-4o-mini": {"input": 0.15, "output": 0.6},
            "gpt-4o": {"input": 2.5, "output": 10.0},
            "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
            "deepseek-chat": {"input": 0.27, "output": 1.1},
        }
        from researchpilot.utils import load_pricing_file, resolve_input_path

        table.update(load_pricing_file(resolve_input_path(self.pricing_file)))
        return table


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip().lower().strip("[]")
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False
