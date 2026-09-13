"""Structural guards for the CI workflow and its local dry-run driver."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPECTED_JOBS = {"lint", "typecheck", "test", "evaluation", "docker"}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_defines_every_required_job() -> None:
    jobs = _workflow()["jobs"]
    assert set(jobs) == EXPECTED_JOBS, f"unexpected CI jobs: {set(jobs) ^ EXPECTED_JOBS}"
    for name, job in jobs.items():
        steps = job.get("steps") or []
        assert steps, f"job {name} has no steps"
    # The docker job is action-only (buildx/push), the rest must run commands.
    for name in ("lint", "typecheck", "test", "evaluation"):
        assert any("run" in step for step in jobs[name]["steps"]), f"job {name} has no run step"


def test_workflow_does_not_mask_failures() -> None:
    for name, job in _workflow()["jobs"].items():
        for step in job.get("steps") or []:
            assert step.get("continue-on-error") is not True, f"{name} masks failures"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "|| true" not in text and "|| exit 0" not in text


def test_workflow_run_steps_reference_existing_targets() -> None:
    """Commands like `python scripts/foo.py` must point at real files."""
    missing: list[str] = []
    for job in _workflow()["jobs"].values():
        for step in job.get("steps") or []:
            command = str(step.get("run", ""))
            for relative in re.findall(r"python (scripts/[A-Za-z0-9_./-]+\.py)", command):
                if not (ROOT / relative).exists():
                    missing.append(relative)
            for source in re.findall(r"(\./)?([A-Za-z0-9_./-]+\.(?:py|yml|yaml))", command):
                candidate = source[1]
                if (
                    candidate.startswith(("researchpilot/", "tests/", "scripts/"))
                    and not (ROOT / candidate).exists()
                ):
                    missing.append(candidate)
    assert not missing, f"CI references missing targets: {sorted(set(missing))}"


def test_local_ci_dry_run_driver_exists_and_is_documented() -> None:
    driver = ROOT / "scripts" / "ci_dry_run.py"
    assert driver.exists()
    assert "main()" in driver.read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ci_dry_run.py" in readme, "the CI dry-run driver must be discoverable"
