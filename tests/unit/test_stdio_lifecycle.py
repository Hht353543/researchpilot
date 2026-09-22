"""The stdio MCP transport owns and reaps its child process."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from researchpilot.mcp.client import McpClientError, StdioMcpClient


def test_stdio_timeout_terminates_and_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    processes: list[object] = []
    real_popen = subprocess.Popen

    def capture_popen(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("researchpilot.mcp.client.subprocess.Popen", capture_popen)
    command = [
        sys.executable,
        "-u",
        "-c",
        "import time; time.sleep(60)",
    ]
    started = time.monotonic()
    with pytest.raises(McpClientError, match="timed out"):
        StdioMcpClient(command=command, timeout_s=0.1)
    assert time.monotonic() - started < 3
    assert processes and processes[0].poll() is not None  # type: ignore[attr-defined]


def test_stdio_abnormal_exit_is_reported_and_reaped() -> None:
    command = [sys.executable, "-u", "-c", "import sys; sys.exit(7)"]
    with pytest.raises(McpClientError, match=r"closed|exited"):
        StdioMcpClient(command=command, timeout_s=1)


def test_stdio_close_uses_kill_after_terminate_grace_period() -> None:
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import signal,time; "
            "signal.signal(signal.SIGTERM, lambda *_: None); "
            'print(\'{"jsonrpc":"2.0","id":1,"result":{}}\', flush=True); '
            "time.sleep(60)"
        ),
    ]
    client = StdioMcpClient(command=command, timeout_s=1, terminate_grace_s=0.1)
    process = client._process
    client.close()
    assert process.poll() is not None
    assert process.returncode is not None
    assert process.returncode != 0


def test_stdio_close_interrupts_an_inflight_read() -> None:
    command = [
        sys.executable,
        "-u",
        "-c",
        ('import time; print(\'{"jsonrpc":"2.0","id":1,"result":{}}\', flush=True); time.sleep(60)'),
    ]
    client = StdioMcpClient(command=command, timeout_s=10, terminate_grace_s=0.2)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            client.list_tools()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=call)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    client.close()
    worker.join(timeout=1)
    assert time.monotonic() - started < 1
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], McpClientError)
