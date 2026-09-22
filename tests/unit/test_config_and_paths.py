"""Configuration, cwd-independent input resolution and the API/frontend contract."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchpilot.config import Settings
from researchpilot.tools.web_backend import build_web_backend
from researchpilot.utils import project_root, resolve_input_path

ROOT = Path(__file__).resolve().parents[2]


def test_project_root_points_at_repository() -> None:
    assert (project_root() / "pyproject.toml").exists()


def test_input_paths_resolve_from_another_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.kb_dir().exists(), "knowledge base must resolve from the project root"
    assert (settings.kb_dir() / "01-agent-architecture.md").exists()
    assert resolve_input_path("eval/golden_dataset.jsonl").exists()
    assert settings.price_table()["gpt-4o-mini"]["output"] > 0


def test_web_backend_finds_corpus_and_reports_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    backend = build_web_backend(Settings())
    assert getattr(backend, "corpus_missing", False) is False
    assert backend.search("MCP gateway", top_k=2)

    missing = build_web_backend(Settings(web_corpus_path=str(tmp_path / "nope")))
    assert getattr(missing, "corpus_missing", False) is True
    assert missing.search("anything", top_k=2) == []


def test_tool_cache_defaults_are_enabled() -> None:
    settings = Settings()
    assert settings.tool_cache_ttl_s > 0, "tool caching must be on by default to be real"


def test_settings_repr_does_not_expose_credentials() -> None:
    settings = Settings(
        _env_file=None,
        api_key="provider-secret",
        access_token="shared-deployment-token",
    )

    rendered = repr(settings)
    assert "provider-secret" not in rendered
    assert "shared-deployment-token" not in rendered


def test_runtime_boundary_defaults_are_local_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RESEARCHPILOT_BIND_HOST",
        "RESEARCHPILOT_API_PORT",
        "RESEARCHPILOT_ALLOW_REMOTE_ACCESS",
        "RESEARCHPILOT_ACCESS_TOKEN",
        "RESEARCHPILOT_MAX_REQUEST_BODY_BYTES",
        "RESEARCHPILOT_MAX_CONCURRENT_TASKS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.bind_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.allow_remote_access is False
    assert settings.access_token is None
    assert settings.max_request_body_bytes > 0
    assert settings.max_concurrent_tasks > 0


def test_remote_binding_requires_explicit_opt_in_and_token() -> None:
    with pytest.raises(ValidationError, match="ALLOW_REMOTE_ACCESS"):
        Settings(_env_file=None, bind_host="0.0.0.0", allow_remote_access=False, access_token=None)
    with pytest.raises(ValidationError, match="ACCESS_TOKEN"):
        Settings(_env_file=None, bind_host="0.0.0.0", allow_remote_access=True, access_token=None)

    shared = Settings(
        _env_file=None,
        bind_host="0.0.0.0",
        allow_remote_access=True,
        access_token="shared-deployment-token",
    )
    assert shared.bind_host == "0.0.0.0"
    assert shared.allow_remote_access is True


def test_shared_deployment_boundary_can_be_enabled_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCHPILOT_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("RESEARCHPILOT_ALLOW_REMOTE_ACCESS", "true")
    monkeypatch.setenv("RESEARCHPILOT_ACCESS_TOKEN", "environment-access-token")

    settings = Settings(_env_file=None)

    assert settings.bind_host == "0.0.0.0"
    assert settings.allow_remote_access is True
    assert settings.access_token == "environment-access-token"


def test_frontend_calls_only_existing_api_routes() -> None:
    """Guards against the frontend calling renamed/removed endpoints."""
    from researchpilot.api.app import create_app

    app = create_app(
        Settings(
            provider="mock", runs_path="runs", kb_path=str(ROOT / "data" / "knowledge_base"), embedding_dim=64
        )
    )
    openapi = app.openapi()
    patterns = [re.sub(r"\{[^}]+\}", "{param}", path) for path in openapi["paths"]]

    script = (ROOT / "researchpilot" / "api" / "static" / "app.js").read_text(encoding="utf-8")
    raw_calls = re.findall(r"api\(\s*[`\"']([^`\"'?]+)", script)
    calls = {re.sub(r"\$\{[^}]+\}", "{param}", call) for call in raw_calls}
    calls = {re.sub(r"\{[^}]+\}", "{param}", call) for call in calls}
    for call in sorted(calls):
        assert call in patterns, f"frontend calls unknown endpoint: {call}"


def test_frontend_exposes_required_settings_fields() -> None:
    html = (ROOT / "researchpilot" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "researchpilot" / "api" / "static" / "app.js").read_text(encoding="utf-8")
    for field_id in ("model", "temperature", "presence", "frequency", "maxtokens", "accessToken"):
        assert f'id="{field_id}"' in html, f"missing settings input: {field_id}"
        assert f'el("{field_id}")' in script, f"settings input {field_id} is never read"
    for panel in ("timeline", "report", "metrics", "kbDocuments", "evaluation"):
        assert f'id="{panel}"' in html, f"missing panel: {panel}"


def test_env_example_documents_every_settings_field() -> None:
    """Any env-overridable setting must be discoverable in .env.example."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^(?:RESEARCHPILOT_)?([A-Z_]+)=", text, re.M))
    optional = {"PROVIDER"}  # documented as RESEARCHPILOT_PROVIDER, alias handled below
    missing = []
    for name in Settings.model_fields:
        upper = name.upper()
        if upper in documented or f"RESEARCHPILOT_{upper}" in documented or upper in optional:
            continue
        missing.append(name)
    assert not missing, f".env.example is missing: {missing}"


@pytest.mark.anyio
async def test_config_endpoint_matches_settings(settings: Settings) -> None:
    """The Settings panel must show the same values the backend actually uses."""
    import httpx

    from researchpilot.api.app import create_app

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        payload = (await client.get("/config")).json()
    assert payload["provider"] == settings.provider
    assert payload["temperature"] == settings.temperature
    assert payload["presence_penalty"] == settings.presence_penalty
    assert payload["frequency_penalty"] == settings.frequency_penalty
    assert payload["max_tokens"] == settings.max_tokens
    assert payload["top_k"] == settings.top_k
    assert payload["max_iterations"] == settings.max_iterations
    assert payload["token_budget"] == settings.token_budget
    assert payload["token_budget_live"] == settings.token_budget_live
    assert payload["auth_required"] is False
    assert payload["max_request_body_bytes"] == settings.max_request_body_bytes
    assert payload["max_concurrent_tasks"] == settings.max_concurrent_tasks
    assert payload["default_model"] == app.state.container.provider.model_name()
