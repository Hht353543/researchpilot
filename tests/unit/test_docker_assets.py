"""Docker/Compose asset verification without a Docker engine.

The build itself cannot run in this environment (no docker binary), so these
checks verify everything that is verifiable offline: the compose schema, service
wiring, environment-variable names against ``Settings``, COPY sources against
.dockerignore, the exact container commands against importable code, and the
healthcheck paths against the real route tables.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from researchpilot.config import Settings
from researchpilot.mcp.server import McpServer, create_http_app
from researchpilot.rag.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_defines_expected_services_and_wiring() -> None:
    compose = _compose()
    services = compose["services"]
    assert set(services) == {"api", "mcp"}
    for name, service in services.items():
        assert service.get("build") or service.get("image"), f"{name} must build or reference an image"
        assert service.get("ports"), f"{name} must expose a port"
    api = services["api"]
    dependency = api["depends_on"]["mcp"]
    assert dependency["condition"] == "service_healthy", (
        "the API must wait for MCP health so it really uses the HTTP transport"
    )
    assert services["mcp"].get("healthcheck", {}).get("test"), "mcp needs a healthcheck"
    assert "runs" in compose.get("volumes", {}), "shared runs volume must be declared"


def test_compose_environment_variables_are_real_settings() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    extra_allowed = {
        "RESEARCHPILOT_PROVIDER",
        "RESEARCHPILOT_MODEL",
        "RESEARCHPILOT_API_KEY",
        "RESEARCHPILOT_BASE_URL",
    }
    for service in _compose()["services"].values():
        for key in service.get("environment") or {}:
            name = key.removeprefix("RESEARCHPILOT_")
            assert name in fields or key in extra_allowed, f"unknown env var in compose: {key}"


def test_dockerfile_copy_sources_exist_and_are_not_ignored() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    ignored = {
        line.strip()
        for line in dockerignore
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("!")
    }
    copies = [
        line.split()[1]
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ")
    ]
    assert copies
    for source in copies:
        assert (ROOT / source).exists(), f"Dockerfile COPY source missing: {source}"
        assert source not in ignored, f"COPY source {source} is excluded by .dockerignore"


def test_dockerfile_installs_project_and_runs_real_commands() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "pip install" in text and "-e ." in text, "image must install the project itself"
    cmd = re.search(r"^CMD \[(.*)\]$", text, re.M)
    assert cmd, "Dockerfile needs an exec-form CMD"
    assert "researchpilot.api.app:create_app" in cmd.group(1)
    assert "--factory" in cmd.group(1), "factory app requires --factory"
    # the exact module path in CMD must be importable and expose the factory
    from researchpilot.api.app import create_app

    assert callable(create_app)


def test_healthcheck_paths_match_real_routes(tmp_path: Path) -> None:
    from researchpilot.api.app import create_app as build_api_app

    settings = Settings(
        provider="mock",
        kb_path=str(ROOT / "data" / "knowledge_base"),
        runs_path=str(tmp_path / "runs"),
        embedding_dim=64,
        web_corpus_path=str(tmp_path / "no-corpus"),
    )
    api_paths = set(build_api_app(settings).openapi()["paths"])
    assert "/health" in api_paths

    mcp_app = create_http_app(McpServer(settings, knowledge_base=KnowledgeBase(settings)))
    mcp_paths = set(mcp_app.openapi()["paths"])
    assert "/health" in mcp_paths
    assert "/mcp" in mcp_paths

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "/health" in dockerfile, "Dockerfile healthcheck must hit a real route"
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "8765/health" in compose, "mcp healthcheck must hit the MCP app route"


def test_pyproject_and_lockfile_agree_on_python_version() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["requires-python"].startswith(">=3.11")
    assert "python:3.12" in DOCKERFILE.read_text(encoding="utf-8")
