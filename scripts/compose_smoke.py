"""Engine-free docker-compose smoke test.

This machine has no container runtime, so ``docker compose up`` cannot run here.
This script reproduces the *compose topology* natively, driven by the real
``docker-compose.yml`` file:

1. parse the compose file (services, commands, ports, environment, depends_on);
2. resolve ``${VAR:-default}`` interpolation exactly like compose does;
3. start the ``mcp`` service first and wait for its healthcheck (mirroring
   ``depends_on: condition: service_healthy``);
4. start the ``api`` service with the compose environment applied;
5. assert the runtime contract: MCP healthy, API healthy, ``/health`` reports the
   HTTP MCP transport, a research run actually calls MCP over HTTP, and the
   frontend is served;
6. tear both services down.

Only container-internal paths/hostnames are remapped to host equivalents
(``/app/...`` -> the repository, ``mcp:8765`` -> ``127.0.0.1:<port>``); every
remap is logged so the evidence stays transparent.

    python scripts/compose_smoke.py
    python scripts/compose_smoke.py --compose docker-compose.yml --timeout 180
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def interpolate(value: str, environ: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return environ.get(name, default if default is not None else "")

    return INTERP_RE.sub(replace, value)


def free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, *, timeout: float, process: subprocess.Popen[str]) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = "not started"
    while time.time() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise RuntimeError(
                f"service exited early (code {process.returncode})\nstdout:\n{stdout[-2000:]}\n"
                f"stderr:\n{stderr[-2000:]}"
            )
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("status") in {"ok", None}:
                    return payload
                last_error = f"unexpected health payload: {payload}"
            else:
                last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def start_service(
    name: str,
    command: list[str],
    env: dict[str, str],
    log_dir: Path,
) -> subprocess.Popen[str]:
    merged = {**os.environ, **env}
    merged["PYTHONUNBUFFERED"] = "1"
    merged["PYTHONIOENCODING"] = "utf-8"
    log = (log_dir / f"{name}.log").open("w", encoding="utf-8")
    print(f"[compose-smoke] starting {name}: {' '.join(command)}")
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=merged,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )


def resolve_environment(raw: dict[str, str] | list[str] | None, environ: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            key, _, value = str(item).partition("=")
            result[key.strip()] = interpolate(value, environ)
    elif isinstance(raw, dict):
        for key, value in raw.items():
            text = "" if value is None else str(value)
            result[str(key)] = interpolate(text, environ)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the compose topology without a container engine")
    parser.add_argument("--compose", default=str(ROOT / "docker-compose.yml"))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--keep-logs", action="store_true")
    args = parser.parse_args()

    compose = yaml.safe_load(Path(args.compose).read_text(encoding="utf-8"))
    services: dict[str, Any] = compose["services"]
    environ = {**os.environ, "RESEARCHPILOT_PROVIDER": os.environ.get("RESEARCHPILOT_PROVIDER", "mock")}
    log_dir = ROOT / "runs_compose_smoke"
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # --- resolve ports ------------------------------------------------------ #
    mcp_port = free_port(int(str(services["mcp"]["ports"][0]).split(":")[0]))
    api_port = free_port(int(str(services["api"]["ports"][0]).split(":")[0]))
    print(f"[compose-smoke] host ports -> mcp={mcp_port} api={api_port}")

    remaps = {
        "http://mcp:8765/mcp": f"http://127.0.0.1:{mcp_port}/mcp",
        "/app/data/knowledge_base": str(ROOT / "data" / "knowledge_base"),
        "/app/runs": str(log_dir / "runs"),
        "/app/eval": str(ROOT / "eval"),
    }

    def remap(env: dict[str, str]) -> dict[str, str]:
        """Container-internal hostnames/paths -> host equivalents (logged)."""
        out: dict[str, str] = {}
        for key, value in env.items():
            new_value = value
            for container_value, host_value in remaps.items():
                if container_value in new_value:
                    new_value = new_value.replace(container_value, host_value)
            if new_value != value:
                print(f"[compose-smoke] remap {key}: {value} -> {new_value}")
            out[key] = new_value
        return out

    mcp_env = remap(resolve_environment(services["mcp"].get("environment"), environ))
    mcp_env.update(
        {
            "RESEARCHPILOT_MCP_TRANSPORT": "http",
            "RESEARCHPILOT_MCP_HOST": "127.0.0.1",
            "RESEARCHPILOT_MCP_PORT": str(mcp_port),
        }
    )
    api_env = remap(resolve_environment(services["api"].get("environment"), environ))
    api_env.update(
        {"RESEARCHPILOT_MCP_TRANSPORT": "http", "RESEARCHPILOT_MCP_URL": f"http://127.0.0.1:{mcp_port}/mcp"}
    )

    python = shutil.which("python") or sys.executable
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    try:
        # --- mcp service (started first: api depends_on service_healthy) ----- #
        mcp_command = [python, "-m", "researchpilot.mcp_server"]
        processes.append(("mcp", start_service("mcp", mcp_command, mcp_env, log_dir)))
        mcp_health = wait_for(
            f"http://127.0.0.1:{mcp_port}/health", timeout=args.timeout, process=processes[-1][1]
        )
        print(f"[compose-smoke] mcp healthy: tools={len(mcp_health.get('tools', []))}")

        # --- api service ----------------------------------------------------- #
        api_command = [
            python,
            "-m",
            "uvicorn",
            "researchpilot.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ]
        processes.append(("api", start_service("api", api_command, api_env, log_dir)))
        api_health = wait_for(
            f"http://127.0.0.1:{api_port}/health", timeout=args.timeout, process=processes[-1][1]
        )
        kb_docs = api_health.get("knowledge_base", {}).get("documents")
        print(
            f"[compose-smoke] api healthy: provider={api_health.get('provider')} "
            f"mcp_transport={api_health.get('mcp_transport')} kb_docs={kb_docs}"
        )
        failures: list[str] = []

        # Run the *image's own* HEALTHCHECK command (from the Dockerfile) against
        # the live API, so "the healthcheck works" is measured, not assumed.
        healthcheck = run_dockerfile_healthcheck(api_port)
        print(f"[compose-smoke] docker HEALTHCHECK command -> exit {healthcheck['code']}")
        if healthcheck["code"] != 0:
            failures.append(
                f"Dockerfile HEALTHCHECK failed (exit {healthcheck['code']}): {healthcheck['output']}"
            )

        if api_health.get("mcp_transport") != "http":
            failures.append(
                f"api did not use the HTTP MCP transport (got {api_health.get('mcp_transport')!r})"
            )

        tools = httpx.get(f"http://127.0.0.1:{api_port}/mcp/tools", timeout=20).json()
        print(f"[compose-smoke] api -> mcp tools endpoint: transport={tools.get('transport')}")
        if tools.get("transport") != "http":
            failures.append(f"/mcp/tools reported transport {tools.get('transport')!r}")

        frontend = httpx.get(f"http://127.0.0.1:{api_port}/", timeout=20)
        print(f"[compose-smoke] frontend: HTTP {frontend.status_code} ({len(frontend.text)} bytes)")
        if frontend.status_code != 200 or "ResearchPilot" not in frontend.text:
            failures.append("frontend index was not served")

        research = httpx.post(
            f"http://127.0.0.1:{api_port}/research",
            json={
                "question": "如何通过 MCP 网关把知识库复用到多个 Agent？",
                "settings": {"max_iterations": 1},
            },
            timeout=300,
        ).json()
        mcp_calls = research.get("metrics", {}).get("mcp_calls", 0)
        print(
            f"[compose-smoke] research: status={research.get('status')} mcp_calls={mcp_calls} "
            f"evidence={len(research.get('evidence', {}).get('evidence', []))}"
        )
        if mcp_calls < 1:
            failures.append("research run did not call MCP over HTTP")
        if research.get("status") not in {"succeeded", "degraded"}:
            failures.append(f"unexpected research status: {research.get('status')}")

        if failures:
            print("\n[compose-smoke] FAILED:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print(
            "\n[compose-smoke] OK: compose topology verified without a container engine\n"
            + json.dumps(
                {
                    "services": sorted(services),
                    "api_port": api_port,
                    "mcp_port": mcp_port,
                    "api_mcp_transport": api_health.get("mcp_transport"),
                    "research_status": research.get("status"),
                    "research_mcp_calls": mcp_calls,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        for name, process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except Exception:  # pragma: no cover
                    process.kill()
            print(f"[compose-smoke] stopped {name}")
        if not args.keep_logs:
            shutil.rmtree(log_dir, ignore_errors=True)
        else:
            print(f"[compose-smoke] logs kept in {log_dir}")


def run_dockerfile_healthcheck(port: int) -> dict[str, Any]:
    """Execute the Dockerfile's healthcheck command against a locally running API."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"CMD\s+(python\s+-c\s+\".*?\")", text)
    if match is None:
        return {"code": 127, "output": "no CMD python -c healthcheck found in the Dockerfile"}
    command = match.group(1)
    if "8000" in command:
        command = command.replace("8000", str(port))
    completed = subprocess.run(
        command,
        shell=True,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    return {
        "code": completed.returncode,
        "output": (completed.stdout + completed.stderr).strip()[-300:],
    }


if __name__ == "__main__":
    raise SystemExit(main())
