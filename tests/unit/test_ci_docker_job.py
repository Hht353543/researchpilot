"""Guards for the CI Docker job.

GitHub Actions is the acceptance environment for the container story (this machine
has no working Docker Engine), so the workflow itself is now part of the
correctness surface. These checks pin the properties that would otherwise rot
quietly:

* every job runs on a Linux runner;
* the ``docker`` job exists, is unconditional, and cannot pass without an engine
  (no ``if: false``, no ``continue-on-error``, no mock stand-in);
* it builds with the project's own ``Dockerfile``, starts the project's own
  ``docker-compose.yml``, inspects the real container health status, verifies the
  API and MCP services over the network, and runs a smoke test inside the
  container.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
SMOKE_SCRIPT = ROOT / "scripts" / "container_smoke.py"

# Steps that exist to collect evidence or clean up must not gate the job; every
# other step has to run unconditionally.
ALLOWED_CONDITIONALS = {"always()", "failure()", "success()"}


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "ci.yml must parse to a mapping"
    return data


def _jobs() -> dict[str, Any]:
    jobs = _workflow().get("jobs") or {}
    assert jobs, "ci.yml defines no jobs"
    return dict(jobs)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps") or [])


def _commands(job: dict[str, Any]) -> list[str]:
    return [str(step["run"]) for step in _steps(job) if "run" in step]


def _docker_job() -> dict[str, Any]:
    jobs = _jobs()
    assert "docker" in jobs, "the docker job was removed from ci.yml"
    return dict(jobs["docker"])


def test_every_ci_job_uses_a_linux_runner() -> None:
    """Requirement: the container story is validated on Linux runners."""
    for name, job in _jobs().items():
        runner = str(job.get("runs-on", ""))
        assert runner.startswith("ubuntu-"), f"job {name!r} does not use a Linux runner: {runner!r}"


def test_docker_job_is_unconditional_and_cannot_silently_skip() -> None:
    """Requirement: a broken container fails the workflow instead of being skipped."""
    job = _docker_job()
    assert "if" not in job, "the docker job must not be conditional"
    assert "continue-on-error" not in job, "the docker job must not be allowed to fail quietly"
    assert job.get("timeout-minutes"), "the docker job needs a timeout so a hang cannot pass"

    for name, candidate in _jobs().items():
        assert "continue-on-error" not in candidate, f"job {name!r} may fail quietly"
        for step in _steps(candidate):
            assert "continue-on-error" not in step, f"step {step.get('name')!r} may fail quietly"
            condition = step.get("if")
            if condition is not None:
                assert str(condition).strip() in ALLOWED_CONDITIONALS, (
                    f"step {step.get('name')!r} has a gate-changing condition: {condition!r}"
                )


def test_docker_job_builds_the_real_image_from_the_project_dockerfile() -> None:
    """Requirement: real ``docker build`` using the repository Dockerfile."""
    commands = " \n".join(_commands(_docker_job()))
    assert "docker build" in commands, "the docker job no longer runs a real docker build"
    assert "--file Dockerfile" in commands, "the build must name the project Dockerfile"
    assert "researchpilot:latest" in commands, "the image must be tagged so compose reuses it"
    assert DOCKERFILE.exists(), "Dockerfile is missing from the repository"
    assert "FROM python:3.12" in DOCKERFILE.read_text(encoding="utf-8")


def test_docker_job_starts_the_project_compose_file() -> None:
    """Requirement: real ``docker compose up`` driven by docker-compose.yml."""
    job = _docker_job()
    commands = " \n".join(_commands(job))
    assert "docker compose" in commands and "up" in commands, "the docker job must start compose"
    assert "--wait" in commands, "compose must wait for healthchecks instead of racing them"
    names_compose_file = "docker-compose.yml" in commands or "docker-compose.yml" in str(job.get("env") or {})
    assert names_compose_file, "the job must name the repository's docker-compose.yml"
    assert COMPOSE.exists(), "docker-compose.yml is missing from the repository"


def test_docker_job_checks_container_health() -> None:
    """Requirement: execute the container health check for both services."""
    commands = [command for command in _commands(_docker_job()) if "docker inspect" in command]
    assert commands, "the docker job does not inspect container health"
    joined = " \n".join(commands)
    assert "Health" in joined, "the health inspection must read the container health state"
    for service in ("mcp", "api"):
        assert service in joined, f"the health check skips the {service} service"


def test_docker_job_verifies_api_and_mcp_and_smokes_inside_the_container() -> None:
    """Requirements: verify both services over the real network, plus an
    in-container smoke test."""
    commands = _commands(_docker_job())
    host_checks = [
        command
        for command in commands
        if "container_smoke.py" in command and "--api-url" in command and "--mcp-url" in command
    ]
    in_container = [
        command for command in commands if "container_smoke.py" in command and "--in-container" in command
    ]
    assert host_checks, "no smoke test runs against the published API/MCP ports"
    assert in_container, "no smoke test runs inside the container"
    assert any("docker compose exec" in command for command in in_container), (
        "the in-container smoke test must be executed through `docker compose exec`"
    )
    assert SMOKE_SCRIPT.exists(), "scripts/container_smoke.py is missing"
    smoke = SMOKE_SCRIPT.read_text(encoding="utf-8")
    assert "/health" in smoke and "/mcp" in smoke, "the smoke script must exercise the real endpoints"


def test_docker_job_only_uses_real_tooling() -> None:
    """No mock engine and no stand-in for the container runtime."""
    job = _docker_job()
    text = " \n".join(_commands(job) + [str(step.get("uses", "")) for step in _steps(job)])
    for stand_in in ("podman", "nerdctl", "mock docker", "fake docker", "act ", "dry-run"):
        assert stand_in not in text, f"the docker job uses a stand-in: {stand_in!r}"
    assert "docker compose" in text and "config" in text, "compose must at least be validated"


# --------------------------------------------------------------------------- #
# Local mirror (`scripts/ci_dry_run.py`): asking for the docker job without an
# engine is a hard error, so nobody can mistake a skip for a pass.
# --------------------------------------------------------------------------- #
def _dry_run() -> Any:
    return importlib.import_module("scripts.ci_dry_run")


def test_local_dry_run_refuses_to_skip_the_docker_job_without_an_engine(
    monkeypatch: Any, capsys: Any
) -> None:
    module = _dry_run()
    monkeypatch.setattr(module, "docker_engine_available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["ci_dry_run.py", "--job", "docker"])
    assert module.main() == 2, "requesting the docker job without an engine must fail"
    captured = capsys.readouterr().out
    assert "no container engine" in captured
    assert "never skipped" in captured


def test_local_dry_run_require_docker_engine_flag_fails_without_one(monkeypatch: Any) -> None:
    module = _dry_run()
    monkeypatch.setattr(module, "docker_engine_available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["ci_dry_run.py", "--job", "docker", "--require-docker-engine"])
    assert module.main() == 2


def test_local_dry_run_lists_the_real_docker_steps_when_an_engine_exists(
    monkeypatch: Any, capsys: Any
) -> None:
    module = _dry_run()
    monkeypatch.setattr(module, "docker_engine_available", lambda: True)
    monkeypatch.setattr(sys, "argv", ["ci_dry_run.py", "--with-docker", "--list"])
    assert module.main() == 0
    captured = capsys.readouterr().out
    assert "[docker] $" in captured
    assert "docker build --file Dockerfile" in captured
    assert "docker compose --file docker-compose.yml up" in captured
    assert "container_smoke.py --in-container" in captured


def test_local_dry_run_refuses_to_recurse_into_pytest(monkeypatch: Any, capsys: Any) -> None:
    """Running every job from inside pytest would re-run the suite; it must refuse."""
    module = _dry_run()
    monkeypatch.setattr(module, "docker_engine_available", lambda: False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/unit/test_ci_docker_job.py::x (call)")
    monkeypatch.setattr(sys, "argv", ["ci_dry_run.py"])
    assert module.main() == 2
    assert "recursively" in capsys.readouterr().out
