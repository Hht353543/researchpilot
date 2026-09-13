"""Regression matrix for the Windows stdio encoding bug.

Root cause (measured): a child Python process whose stdio is a pipe defaults to the
OS locale encoding (``cp936``/``gbk`` here) with ``surrogateescape``, while the MCP
client speaks UTF-8. Chinese arguments were mangled on the way in (Pydantic then
reported invalid unicode) and the response bytes were not valid UTF-8 on the way
out -> ``UnicodeDecodeError`` inside ``readline()``.

Covered here: Chinese query + Chinese result, English query, Chinese validation
error, malformed arguments, unknown tool, tool exceptions, stdout purity, and the
server working with *no* environment help at all.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from researchpilot.config import Settings
from researchpilot.mcp.client import McpClientError, StdioMcpClient
from researchpilot.mcp.server import McpServer, serve_stdio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SERVER_CMD = [sys.executable, "-m", "researchpilot.mcp_server", "--stdio"]
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stdio_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the child server at the fixture knowledge base (inherited via env)."""
    monkeypatch.setenv("RESEARCHPILOT_KB_PATH", str(FIXTURES / "kb"))
    monkeypatch.setenv("RESEARCHPILOT_RUNS_PATH", str(settings.runs_dir()))
    monkeypatch.setenv("RESEARCHPILOT_PROVIDER", "mock")
    monkeypatch.setenv("RESEARCHPILOT_EMBEDDING_DIM", "64")
    # Deliberately remove the "helpful" variables: the server must fix itself.
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)


def test_chinese_query_and_chinese_result(stdio_env: None) -> None:
    """The exact call from the bug report must work and return Chinese content."""
    client = StdioMcpClient(timeout_s=60)
    try:
        result = client.call_tool("search_knowledge", {"query": "工具注册表", "top_k": 2})
        structured = result["structuredContent"]
        assert result["isError"] is False
        assert structured["query"] == "工具注册表", structured["query"]
        assert structured["items"], "expected knowledge-base hits"
        joined = " ".join(item["title"] + item["content"] for item in structured["items"])
        assert any(marker in joined for marker in ("注册表", "权限", "工具", "MCP")), joined[:200]
    finally:
        client.close()


def test_english_query_still_works(stdio_env: None) -> None:
    client = StdioMcpClient(timeout_s=60)
    try:
        result = client.call_tool("search_knowledge", {"query": "MCP protocol", "top_k": 2})
        assert result["isError"] is False
        assert result["structuredContent"]["items"]
    finally:
        client.close()


def test_chinese_validation_error_keeps_the_text(stdio_env: None) -> None:
    """A rejected Chinese argument must be echoed readably, not as mojibake."""
    client = StdioMcpClient(timeout_s=60)
    try:
        with pytest.raises(McpClientError) as excinfo:
            client.call_tool("search_knowledge", {"query": "工具注册表", "strategy": "非法策略"})
        assert "非法策略" in str(excinfo.value)
    finally:
        client.close()


def test_malformed_arguments_and_unknown_tool(stdio_env: None) -> None:
    client = StdioMcpClient(timeout_s=60)
    try:
        with pytest.raises(McpClientError):
            client.call_tool("search_knowledge", {"query": "工具注册表", "top_k": 999})
        with pytest.raises(McpClientError):
            client.call_tool("search_knowledge", {})  # query is required
        with pytest.raises(McpClientError):
            client.call_tool("does_not_exist", {})
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# Server-side independence: no -X utf8, no PYTHONIOENCODING, raw binary pipes
# --------------------------------------------------------------------------- #
class _EchoArgs(BaseModel):
    text: str = "x"


def _raising_server() -> McpServer:
    """A server with one always-failing tool (no knowledge base needed)."""
    server = McpServer.__new__(McpServer)
    server.settings = Settings(provider="mock", embedding_dim=64)
    server.knowledge_base = None  # type: ignore[assignment]
    # Deliberately bypass the attribute type: this bare server never touches the
    # knowledge base or the web backend (it only serves the failing tool).
    object.__setattr__(server, "web_backend", None)
    server._tools = {}
    server.calls = {}

    def boom(args: BaseModel) -> dict[str, Any]:
        raise RuntimeError("工具内部错误：无法处理该请求")

    server._add("boom", "always raises", _EchoArgs, boom)
    return server


def test_tool_exception_is_returned_as_structured_error() -> None:
    """An exception inside a tool must come back as JSON-RPC isError, not crash."""
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "boom", "arguments": {"text": "中文参数"}},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    stdout = io.StringIO()
    serve_stdio(_raising_server(), stdin=stdin, stdout=stdout)
    payload = json.loads(stdout.getvalue().strip())
    assert payload["result"]["isError"] is True
    assert "工具内部错误" in payload["result"]["structuredContent"]["error"]


def test_in_memory_stdio_keeps_chinese_intact() -> None:
    """The StringIO path (no ``.buffer``) must also stay UTF-8 clean."""
    stdin = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "initialize",
                "params": {"clientInfo": {"name": "中文客户端"}},
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    stdout = io.StringIO()
    serve_stdio(_raising_server(), stdin=stdin, stdout=stdout)
    payload = json.loads(stdout.getvalue().strip())
    assert payload["result"]["serverInfo"]["name"] == "researchpilot-mcp"


def _raw_server_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONIOENCODING", "PYTHONUTF8"}}
    env.update(
        {
            "RESEARCHPILOT_KB_PATH": str(FIXTURES / "kb"),
            "RESEARCHPILOT_PROVIDER": "mock",
            "RESEARCHPILOT_EMBEDDING_DIM": "64",
        }
    )
    env.update(extra)
    return env


def _spawn_raw_server(tmp_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_raw_server_env(RESEARCHPILOT_RUNS_PATH=str(tmp_path / "runs")),
        cwd=str(REPO_ROOT),
    )


def test_server_emits_valid_utf8_without_any_env_help(tmp_path: Path) -> None:
    """The server alone must be correct even though this machine's locale is cp936."""
    process = _spawn_raw_server(tmp_path)
    try:
        assert process.stdin is not None and process.stdout is not None
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "工具注册表", "top_k": 2}},
        }
        process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()
        raw = process.stdout.readline()
        text = raw.decode("utf-8", errors="strict")  # used to raise UnicodeDecodeError
        payload = json.loads(text)
        assert payload["result"]["isError"] is False
        assert payload["result"]["structuredContent"]["query"] == "工具注册表"
        assert "\r" not in text, "protocol lines must use a plain \\n terminator"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:  # pragma: no cover
            process.kill()


def test_server_stdout_carries_protocol_messages_only(tmp_path: Path) -> None:
    """Startup logs and debug output must go to stderr, never to the protocol stream."""
    process = _spawn_raw_server(tmp_path)
    try:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        for request in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.write(b"{not json\n")
        process.stdin.flush()
        lines = [process.stdout.readline() for _ in range(3)]
        payloads = [json.loads(line.decode("utf-8", errors="strict")) for line in lines]
        assert all(payload["jsonrpc"] == "2.0" for payload in payloads)
        assert payloads[2]["error"]["code"] == -32700
        process.terminate()
        process.wait(timeout=10)
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        assert "researchpilot-mcp" in stderr, "the startup notice belongs on stderr"
    finally:
        if process.poll() is None:  # pragma: no cover
            process.terminate()


def test_client_launches_child_with_explicit_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural guard: the client must not rely on the parent's locale."""
    captured: dict[str, Any] = {}
    initialize_line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
    ).encode("utf-8")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = io.StringIO()
            self.stdout = io.BytesIO(initialize_line + b"\n")
            self.stderr = io.StringIO()

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("researchpilot.mcp.client.subprocess.Popen", fake_popen)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    client = StdioMcpClient()
    assert "-X" in captured["command"] and "utf8" in captured["command"]
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "strict"
    assert captured["kwargs"]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["kwargs"]["env"]["PYTHONUTF8"] == "1"
    client.close()


def test_stdio_client_is_a_context_manager(stdio_env: None) -> None:
    """Documented usage (`with StdioMcpClient() as client:`) must clean up the child."""
    with StdioMcpClient(timeout_s=60) as client:
        assert client.list_tools()
        process = client._process
    assert process.poll() is not None, "leaving the context must terminate the server"
