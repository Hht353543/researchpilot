"""Exercise ``scripts/container_smoke.py`` against a real, running stack.

The CI ``docker`` job runs this script against the containerised stack started by
``docker compose up --wait``. This machine has no container engine, so the fixture
below starts the *same two services* natively (honouring the environment and the
command lines from ``docker-compose.yml``) and points the script at them.

That proves the gate really works - both that it passes on a healthy stack and
that it fails loudly on a broken one - before it is allowed to gate a real build.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SCRIPT = ROOT / "scripts" / "container_smoke.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, *, timeout: float) -> None:
    deadline = time.time() + timeout
    last = "not started"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
                last = f"HTTP {response.status}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise AssertionError(f"{url} never became healthy: {last}")


@pytest.fixture(scope="module")
def running_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Start the mcp + api services natively, in compose order (mcp first)."""
    workdir = tmp_path_factory.mktemp("container_smoke")
    mcp_port, api_port = _free_port(), _free_port()
    base_env = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "RESEARCHPILOT_PROVIDER": "mock",
        "RESEARCHPILOT_EMBEDDING_DIM": "64",
        "RESEARCHPILOT_KB_PATH": str(FIXTURES / "kb"),
        "RESEARCHPILOT_RUNS_PATH": str(workdir / "runs"),
    }
    processes: list[subprocess.Popen[str]] = []
    try:
        mcp_env = {
            **base_env,
            "RESEARCHPILOT_MCP_TRANSPORT": "http",
            "RESEARCHPILOT_MCP_HOST": "127.0.0.1",
            "RESEARCHPILOT_MCP_PORT": str(mcp_port),
        }
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "researchpilot.mcp_server"],
                cwd=str(ROOT),
                env=mcp_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
        )
        _wait_for(f"http://127.0.0.1:{mcp_port}/health", timeout=90)

        api_env = {
            **base_env,
            "RESEARCHPILOT_MCP_TRANSPORT": "http",
            "RESEARCHPILOT_MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp",
        }
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "researchpilot.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=str(ROOT),
                env=api_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
        )
        _wait_for(f"http://127.0.0.1:{api_port}/health", timeout=90)

        yield {
            "api": f"http://127.0.0.1:{api_port}",
            "mcp": f"http://127.0.0.1:{mcp_port}",
            "runs": str(workdir / "runs"),
        }
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    process.kill()


def _run_smoke(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _run_smoke_with_env(
    extra_env: dict[str, str], *args: str, timeout: float = 600.0
) -> subprocess.CompletedProcess[str]:
    """Same as :func:`_run_smoke`, with extra variables for the ``--in-container`` checks."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        env={**os.environ, **extra_env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def test_container_smoke_script_passes_on_a_healthy_stack(
    running_stack: dict[str, str], tmp_path: Path
) -> None:
    report = tmp_path / "container-smoke.json"
    result = _run_smoke(
        "--api-url",
        running_stack["api"],
        "--mcp-url",
        running_stack["mcp"],
        "--json-out",
        str(report),
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary["result"] == "pass"
    assert summary["failures"] == []
    assert summary["api"]["mcp_transport"] == "http"
    assert summary["mcp_chinese_query"]["echoed"] == "工具注册表"
    assert summary["mcp_chinese_query"]["hits"] > 0
    assert summary["research"]["mcp_calls"] >= 1
    assert summary["research"]["sources"] >= 1


def test_container_smoke_script_fails_when_the_api_is_unreachable(
    running_stack: dict[str, str],
) -> None:
    """A dead port must fail the gate, not be reported as a pass."""
    dead = f"http://127.0.0.1:{_free_port()}"
    result = _run_smoke("--api-url", dead, "--mcp-url", running_stack["mcp"], "--timeout", "3")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "FAILED" in combined


def test_in_container_mode_gates_on_the_packaged_knowledge_base(
    running_stack: dict[str, str], tmp_path: Path
) -> None:
    """``--in-container`` must fail when the image is missing its data, and pass when it is there."""
    stack_args = (
        "--api-url",
        running_stack["api"],
        "--mcp-url",
        running_stack["mcp"],
        "--in-container",
        "--timeout",
        "5",
    )
    present = _run_smoke_with_env(
        {
            "RESEARCHPILOT_KB_PATH": str(FIXTURES / "kb"),
            "RESEARCHPILOT_RUNS_PATH": str(tmp_path / "runs"),
        },
        *stack_args,
    )
    assert present.returncode == 0, present.stdout + present.stderr
    assert "packaged knowledge base present" in present.stdout

    absent = _run_smoke_with_env(
        {
            "RESEARCHPILOT_KB_PATH": str(tmp_path / "absent"),
            "RESEARCHPILOT_RUNS_PATH": str(tmp_path / "runs"),
        },
        *stack_args,
    )
    combined = absent.stdout + absent.stderr
    assert absent.returncode == 1, combined
    assert "packaged knowledge base" in combined
