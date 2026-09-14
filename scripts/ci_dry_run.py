"""Execute the commands of .github/workflows/ci.yml locally, job by job.

GitHub Actions cannot run in this environment, so this script parses the *actual*
workflow file and runs every ``run:`` step of the reproducible jobs (lint,
typecheck, test, evaluation) in the repository root. ``uses:`` steps (checkout,
setup-python, upload-artifact) are reported and skipped with an explicit reason.

The ``docker`` job is the one GitHub Actions owns: this machine has no working
Docker Engine, so the job runs for real on the ``ubuntu-latest`` runner. Locally it
is *never* silently skipped - asking for it without an engine is a hard error
(exit 2), and with an engine it is executed like any other job:

    python scripts/ci_dry_run.py                       # reproducible jobs (+ docker notice)
    python scripts/ci_dry_run.py --job test            # a single job
    python scripts/ci_dry_run.py --with-docker         # also run the docker job (needs an engine)
    python scripts/ci_dry_run.py --require-docker-engine   # fail if no engine is available
    python scripts/ci_dry_run.py --list                # show what would run

Exit code 0 only if every executed step succeeded - the same gate CI enforces.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# Run as `python scripts/ci_dry_run.py`: make the package importable from the repo.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from researchpilot.utils import configure_script_stdio  # noqa: E402

# Jobs whose steps only need the local toolchain; `docker` needs an engine.
REPRODUCIBLE_JOBS = ("lint", "typecheck", "test", "evaluation")
DOCKER_JOB = "docker"
ENGINE_DEPENDENT_JOBS = {DOCKER_JOB: "no container engine (docker build / docker compose up)"}


def docker_engine_available() -> bool:
    """True when a real engine answers, so the docker job can run here too."""
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def report_missing_engine(job_name: str) -> None:
    print(f"[ci-dry-run] {job_name}: {ENGINE_DEPENDENT_JOBS[job_name]}")
    print(
        "[ci-dry-run] this job runs for real in .github/workflows/ci.yml on the "
        "ubuntu-latest runner; it is never skipped there."
    )
    print("[ci-dry-run] on a machine with a working engine, pass --with-docker to run it here too.")


def inside_pytest() -> bool:
    """True when this script is executing inside a pytest process."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def load_workflow(path: Path = WORKFLOW) -> dict[str, Any]:
    in_workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if in_workflow is None:
        raise ValueError(f"empty workflow: {path}")
    return in_workflow


def run_steps(job_name: str, job: dict[str, Any], *, list_only: bool = False) -> tuple[int, int]:
    failures = 0
    executed = 0
    env = {**os.environ, **(job.get("env") or {})}
    for index, step in enumerate(job.get("steps") or [], start=1):
        if "run" in step:
            command = str(step["run"]).strip()
            label = step.get("name") or command.splitlines()[0][:60]
            if command.startswith("pip install") and not include_install():
                print(
                    f"[ci-dry-run] {job_name}: skipping '{command}' "
                    "(dependency install needs PyPI; verified separately in a clean venv "
                    "and by the Docker build; pass --include-install to run it here)"
                )
                continue
            if list_only:
                # Print the whole command: the container steps are multi-line and
                # the first line is only `set -euo pipefail`.
                print(f"  [{job_name}] $ {command}")
                continue
            print(f"[ci-dry-run] {job_name} step {index}: {label}")
            started = time.perf_counter()
            # Commands come from our own workflow file, not from user input.
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(ROOT),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.perf_counter() - started
            executed += 1
            if completed.returncode != 0:
                failures += 1
                print(f"[ci-dry-run] FAILED ({completed.returncode}) after {elapsed:.1f}s: {command[:160]}")
                if not args_keep_going():
                    return failures, executed
            else:
                print(f"[ci-dry-run] ok ({elapsed:.1f}s)")
        elif "uses" in step:
            print(f"[ci-dry-run] {job_name}: skipping action step {step['uses']}")
    return failures, executed


def args_keep_going() -> bool:
    return "--keep-going" in sys.argv


def include_install() -> bool:
    return "--include-install" in sys.argv


def main() -> int:
    configure_script_stdio()
    parser = argparse.ArgumentParser(description="Run the CI workflow steps locally")
    parser.add_argument("--workflow", default=str(WORKFLOW))
    parser.add_argument("--job", action="append", default=None, help="job name (repeatable)")
    parser.add_argument("--list", action="store_true", help="only print the commands")
    parser.add_argument("--keep-going", action="store_true", help="do not stop at the first failure")
    parser.add_argument(
        "--with-docker",
        action="store_true",
        help="also run the docker job (needs a working container engine)",
    )
    parser.add_argument(
        "--require-docker-engine",
        action="store_true",
        help="exit non-zero unless the docker job can really be executed here",
    )
    parser.add_argument(
        "--include-install",
        action="store_true",
        help="also run the workflow's pip install steps (needs network)",
    )
    global _ARGS
    _ARGS = parser.parse_args()

    if inside_pytest() and not (_ARGS.job or _ARGS.list):
        # Running every job from inside a test would re-run the test suite that
        # started us (the `test` job runs pytest). Refuse instead of recursing.
        print(
            "[ci-dry-run] refusing to run the full job set from inside pytest: "
            "that would re-enter the test suite recursively. "
            "Pass --job <name> or --list."
        )
        return 2

    workflow = load_workflow(Path(_ARGS.workflow))
    jobs: dict[str, Any] = workflow.get("jobs") or {}
    selected = list(_ARGS.job or [])
    explicitly_requested = bool(selected)
    if _ARGS.list:
        # `--list` is a read-only preview: show every job in the workflow, including
        # the container job that only GitHub Actions can actually execute.
        selected = list(jobs)
    if not selected:
        selected = [name for name in REPRODUCIBLE_JOBS if name in jobs]
    if _ARGS.with_docker and DOCKER_JOB in jobs and DOCKER_JOB not in selected:
        selected.append(DOCKER_JOB)

    engine_available = True
    if not _ARGS.list:
        engine_available = (
            docker_engine_available() if any(name in ENGINE_DEPENDENT_JOBS for name in selected) else True
        )

    total_failures = 0
    total_steps = 0
    for name in selected:
        if name not in jobs:
            print(f"[ci-dry-run] job {name!r} not found in {_ARGS.workflow}")
            return 2
        if name in ENGINE_DEPENDENT_JOBS and not engine_available:
            # Never silently pass: either run the job for real, or fail loudly.
            report_missing_engine(name)
            if explicitly_requested or _ARGS.with_docker or _ARGS.require_docker_engine:
                print(f"[ci-dry-run] refusing to report job {name!r} as done without an engine")
                return 2
            continue
        print(f"\n=== job: {name} ===")
        failures, executed = run_steps(name, jobs[name], list_only=_ARGS.list)
        total_failures += failures
        total_steps += executed

    if DOCKER_JOB in jobs and DOCKER_JOB not in selected:
        report_missing_engine(DOCKER_JOB)
        if _ARGS.require_docker_engine:
            return 2

    if _ARGS.list:
        print(f"\n[ci-dry-run] listed steps for: {', '.join(selected)}")
        return 0
    status = "FAILED" if total_failures else "OK"
    print(f"\n[ci-dry-run] {status}: {total_steps - total_failures}/{total_steps} executed steps passed")
    return 1 if total_failures else 0


_ARGS: argparse.Namespace | None = None


if __name__ == "__main__":
    raise SystemExit(main())
