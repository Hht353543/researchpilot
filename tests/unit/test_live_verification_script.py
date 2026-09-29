"""The real-model verification script must work offline in its own logic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from researchpilot.config import Settings
from researchpilot.llm.base import LLMResponse
from researchpilot.schemas import TokenUsage

ROOT = Path(__file__).resolve().parents[2]


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "verify_live_model", ROOT / "scripts" / "verify_live_model.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_requires_a_credential(monkeypatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "get_settings", lambda: Settings(api_key=None, provider="mock"))
    settings = script._settings()
    assert settings.api_key in (None, "")
    assert script.probe(settings) == 2


def test_probe_reports_success_with_a_stubbed_provider(monkeypatch, capsys) -> None:
    script = _load_script()

    class StubProvider:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(
                text=json.dumps(
                    {
                        "issues": [],
                        "needs_more_research": False,
                        "follow_up_queries": [],
                        "coverage": {},
                        "overall_assessment": "probe ok",
                    }
                ),
                model=self.settings.model,
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )

    monkeypatch.setattr(script, "OpenAICompatibleProvider", StubProvider)
    settings = Settings(provider="openai", api_key="test-key", model="stub-model")
    assert script.probe(settings) == 0
    captured = capsys.readouterr().out
    assert "probe -> ok" in captured
    assert "test-key" not in captured, "the probe must never print the credential"


def test_settings_uses_documented_env_names(monkeypatch) -> None:
    script = _load_script()
    monkeypatch.setattr(script, "get_settings", lambda: Settings(api_key=None, provider="mock"))
    monkeypatch.setenv("API_KEY", "from-api-key")
    monkeypatch.setenv("MODEL", "env-model")
    monkeypatch.setenv("BASE_URL", "https://example.invalid/v1")
    settings = script._settings()
    assert settings.api_key == "from-api-key"
    assert settings.model == "env-model"
    assert settings.base_url == "https://example.invalid/v1"
    assert settings.provider == "openai"
