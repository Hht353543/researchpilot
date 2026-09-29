"""Engine-free docker-compose topology test.

Runs ``scripts/compose_smoke.py``, which starts the two compose services
(``mcp`` then ``api``, honouring ``depends_on: service_healthy``) as local
processes using the environment/ports from ``docker-compose.yml`` and asserts the
runtime contract: MCP healthy, API healthy with the HTTP MCP transport, frontend
served, and a research run that really calls MCP over HTTP.

Skips when a container engine is present (then `docker compose up` is the real
check) or when the script's ports are unavailable.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.skipif(
    any(shutil.which(engine) for engine in ("docker", "podman")),
    reason="a container engine is available: run `docker compose up` instead",
)
def test_compose_topology_runs_without_container_engine() -> None:
    if not (_port_free(8000) and _port_free(8765)):
        pytest.skip("compose smoke test ports are in use")
    result = subprocess.run(
        ["python", "scripts/compose_smoke.py", "--timeout", "120"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "mcp_transport=http" in combined
    assert '"research_mcp_calls": 1' in combined or "mcp_calls=1" in combined
