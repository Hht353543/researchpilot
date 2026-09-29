"""Run the node-based frontend tests from pytest (skipped when node is absent)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_frontend_render_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", "tests/frontend/**/*.test.mjs"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"
    assert "fail 0" in result.stdout or "# fail 0" in result.stdout, result.stdout
