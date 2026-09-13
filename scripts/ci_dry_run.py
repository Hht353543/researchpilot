"""Execute the commands of .github/workflows/ci.yml locally, job by job.

GitHub Actions cannot run in this environment, so this script parses the *actual*
workflow file and runs every ``run:`` step of the reproducible jobs (lint,
typecheck, test, evaluation) in the repository root. ``uses:`` steps (checkout,
setup-python, upload-artifact) and the ``docker`` job (needs a container engine)
are reported and skipped with an explicit reason.

    python scripts/ci_dry_run.py                # all reproducible jobs
    python scripts/ci_dry_run.py --job test     # a single job
    python scripts/ci_dry_run.py --list         # show what would run

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
SKIPPED_JOBS = {"docker": "requires a container engine (docker buildx)"}


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
                print(f"  [{job_name}] $ {command.splitlines()[0][:100]}")
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
        "--include-install",
        action="store_true",
        help="also run the workflow's pip install steps (needs network)",
    )
    global _ARGS
    _ARGS = parser.parse_args()

    workflow = load_workflow(Path(_ARGS.workflow))
    jobs: dict[str, Any] = workflow.get("jobs") or {}
    selected = _ARGS.job or [name for name in REPRODUCIBLE_JOBS if name in jobs]

    total_failures = 0
    total_steps = 0
    for name in selected:
        if name not in jobs:
            print(f"[ci-dry-run] job {name!r} not found in {_ARGS.workflow}")
            return 2
        print(f"\n=== job: {name} ===")
        failures, executed = run_steps(name, jobs[name], list_only=_ARGS.list)
        total_failures += failures
        total_steps += executed
    for name, reason in SKIPPED_JOBS.items():
        if name in jobs and (not _ARGS.job or name in _ARGS.job):
            print(f"[ci-dry-run] NOTE: job {name!r} skipped - {reason}")

    if _ARGS.list:
        print(f"\n[ci-dry-run] listed steps for: {', '.join(selected)}")
        return 0
    status = "FAILED" if total_failures else "OK"
    print(f"\n[ci-dry-run] {status}: {total_steps - total_failures}/{total_steps} executed steps passed")
    return 1 if total_failures else 0


_ARGS: argparse.Namespace | None = None


if __name__ == "__main__":
    raise SystemExit(main())
