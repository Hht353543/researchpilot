"""Verify that the *committed* repository alone can run the documented workflow.

A reviewer (or GitHub Actions) starts from a fresh checkout: no ``runs/``, no
``.env``, no local artifacts. This script clones the repository into a temporary
directory and runs the CI dry run there, which covers lint, typecheck, unit +
integration tests, the evaluation harness, the golden dataset, the benchmark and
the compose topology. It catches hidden dependencies on untracked state and
``.gitignore`` mistakes that would only show up for a visitor.

    python scripts/verify_fresh_clone.py            # clone + full CI dry run
    python scripts/verify_fresh_clone.py --keep     # keep the clone for inspection
    python scripts/verify_fresh_clone.py --quick    # tests only (skip benchmark/eval)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchpilot.utils import configure_script_stdio

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def main() -> int:
    configure_script_stdio()
    parser = argparse.ArgumentParser(description="Verify the committed tree is self-contained")
    parser.add_argument("--repo", default=str(ROOT))
    parser.add_argument("--keep", action="store_true", help="keep the temporary clone")
    parser.add_argument("--quick", action="store_true", help="only run lint/typecheck/unit+integration")
    args = parser.parse_args()

    target = Path(tempfile.mkdtemp(prefix="rp_fresh_clone_")) / "repo"
    print(f"[fresh-clone] cloning {args.repo} -> {target}")
    code, output = _run(["git", "clone", "--quiet", args.repo, str(target)], cwd=Path(tempfile.gettempdir()))
    if code != 0:
        print(f"[fresh-clone] clone failed:\n{output[-1000:]}")
        return 2

    try:
        tracked = _run(["git", "ls-files"], cwd=target)[1].split()
        runs_dir_exists = (target / "runs").exists()
        print(f"[fresh-clone] tracked files: {len(tracked)} | runs/ present: {runs_dir_exists}")
        if runs_dir_exists:
            print("[fresh-clone] WARNING: runs/ exists in a fresh clone (should be ignored)")

        started = time.perf_counter()
        if args.quick:
            steps = [
                ["python", "-m", "ruff", "check", "."],
                ["python", "-m", "mypy", "researchpilot"],
                ["python", "-m", "pytest", "tests/unit", "tests/integration", "-q"],
            ]
        else:
            steps = [["python", "scripts/ci_dry_run.py"]]
        for step in steps:
            print(f"[fresh-clone] $ {' '.join(step)}")
            code, output = _run(step, cwd=target)
            if code != 0:
                print(f"[fresh-clone] FAILED ({code}):\n{output[-3000:]}")
                return 1
            tail = [line for line in output.strip().splitlines() if line.strip()][-1:]
            print(f"[fresh-clone] ok: {tail[0][:140] if tail else ''}")
        print(f"[fresh-clone] OK in {time.perf_counter() - started:.1f}s")
        return 0
    finally:
        if args.keep:
            print(f"[fresh-clone] clone kept at {target}")
        else:
            shutil.rmtree(target.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
