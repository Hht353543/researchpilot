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


def test_compose_publishes_services_on_loopback_by_default() -> None:
    services = _compose()["services"]
    api_port = str(services["api"]["ports"][0])
    mcp_port = str(services["mcp"]["ports"][0])
    assert "RESEARCHPILOT_BIND_HOST:-127.0.0.1" in api_port
    assert mcp_port.startswith("127.0.0.1:")
    api_env = services["api"]["environment"]
    assert "RESEARCHPILOT_ALLOW_REMOTE_ACCESS" in api_env
    assert "RESEARCHPILOT_ACCESS_TOKEN" in api_env


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
    assert "pip install" in text and "-c constraints.txt ." in text, (
        "image must install the project with the tested constraints"
    )
    assert "-e ." not in text, "the deployment image must exercise a regular package install"
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


# --------------------------------------------------------------------------- #
# Image completeness: every file the container reads at runtime must be inside
# the built image (COPY'd and not excluded by .dockerignore).
# --------------------------------------------------------------------------- #
RUNTIME_INPUTS = [
    # setting -> repo-relative path the app reads at runtime
    "data/knowledge_base/01-agent-architecture.md",
    "data/web_corpus/items.jsonl",
    "configs/pricing.yaml",
    "eval/golden_dataset.jsonl",
    "researchpilot/api/static/index.html",
    "researchpilot/api/static/app.js",
    "researchpilot/api/static/styles.css",
]


def _dockerignore_patterns() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _is_ignored(rel_path: str) -> bool:
    """Docker .dockerignore semantics.

    Docker matches patterns against the path relative to the build-context root
    using Go's filepath.Match: ``*`` does NOT cross ``/`` (so ``*.md`` matches only
    root-level markdown) and ``**`` does. Later rules win and ``!`` negates.
    Python's PurePath.match uses fnmatch semantics where ``*`` crosses ``/``, which
    produced false positives here - hence this explicit converter.
    """
    path = rel_path.lstrip("./")
    ignored = False
    for pattern in _dockerignore_patterns():
        negate = pattern.startswith("!")
        candidate = (pattern[1:] if negate else pattern).strip().lstrip("/").rstrip("/")
        if not candidate:
            continue
        regex = _dockerignore_regex(candidate)
        if re.fullmatch(regex, path) or re.fullmatch(f"{regex}/.*", path):
            ignored = not negate
    return ignored


def _dockerignore_regex(pattern: str) -> str:
    """Translate a .dockerignore pattern into an anchored regex (Go semantics)."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                out.append(".*")
                index += 2
                if pattern[index : index + 1] == "/":
                    index += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def test_every_runtime_input_ships_inside_the_image() -> None:
    copies = [
        line.split()[1]
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("COPY ")
    ]
    for rel_path in RUNTIME_INPUTS:
        local = ROOT / rel_path
        assert local.exists(), f"runtime input missing from the repository: {rel_path}"
        covered = any(
            rel_path == source or rel_path.startswith(f"{source.rstrip('/')}/") for source in copies
        )
        assert covered, f"{rel_path} is not covered by any Dockerfile COPY entry"
        assert not _is_ignored(rel_path), f"{rel_path} would be excluded by .dockerignore"


def test_dockerignore_semantics_are_sane() -> None:
    """Sanity-check the matcher so the completeness test cannot silently pass."""
    assert _is_ignored("researchpilot/__pycache__/x.pyc")
    assert _is_ignored("benchmarks/latest_mock.json")
    assert _is_ignored("docs/architecture.md")
    assert not _is_ignored("README.md")  # negated rule wins
    # `*.md` is root-anchored in Docker semantics: nested markdown still ships.
    assert _is_ignored("CHANGELOG.md")
    assert not _is_ignored("data/knowledge_base/01-agent-architecture.md")
    assert not _is_ignored("researchpilot/api/static/app.js")
