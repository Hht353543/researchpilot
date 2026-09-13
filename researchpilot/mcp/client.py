"""MCP clients: in-process, stdio (subprocess) and HTTP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from researchpilot.config import Settings, get_settings
from researchpilot.mcp.protocol import MCP_PROTOCOL_VERSION


class McpClientError(RuntimeError):
    """Raised when an MCP call fails or times out."""


class InProcessMcpClient:
    """Calls a local :class:`McpServer` through the same JSON-RPC surface."""

    name = "inprocess"

    def __init__(self, server: Any) -> None:
        self.server = server
        self._request_id = 0
        self.initialize()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {}}
        )
        if response is None:
            raise McpClientError(f"no response for {method}")
        if "error" in response:
            raise McpClientError(f"{method} failed: {response['error']}")
        return dict(response["result"])

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "researchpilot-agent"},
            },
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._rpc("tools/list").get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        return None

    def __enter__(self) -> InProcessMcpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class StdioMcpClient:
    """Spawns the MCP server as a subprocess and speaks newline-delimited JSON-RPC."""

    name = "stdio"

    def __init__(self, *, command: list[str] | None = None, timeout_s: float = 20.0) -> None:
        # `-X utf8` + PYTHONIOENCODING make the child's stdio UTF-8 regardless of
        # the OS locale (Windows defaults to cp936 here, which corrupts Chinese
        # arguments in both directions). The server also pins its own streams, so
        # either side alone is enough - this is defence in depth for a subprocess
        # we launch ourselves.
        command = command or [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "researchpilot.mcp_server",
            "--stdio",
        ]
        self.timeout_s = timeout_s
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=env,
            cwd=str(Path.cwd()),
        )
        self._request_id = 0
        self.initialize()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process.stdin is None or self._process.stdout is None:  # pragma: no cover
            raise McpClientError("stdio pipes are not available")
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._process.stdin.flush()
        line = self._process.stdout.readline()
        if not line:
            stderr = self._process.stderr.read() if self._process.stderr else ""
            raise McpClientError(f"MCP server closed the stream. stderr: {stderr[:400]}")
        response = json.loads(line)
        if "error" in response:
            raise McpClientError(f"{method} failed: {response['error']}")
        return dict(response["result"])

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "clientInfo": {"name": "researchpilot-agent"}},
        )
        if self._process.stdin is not None:
            self._process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            )
            self._process.stdin.flush()
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._rpc("tools/list").get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except Exception:  # pragma: no cover
                self._process.kill()

    def __enter__(self) -> StdioMcpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class HttpMcpClient:
    """Talks to the MCP server over streamable HTTP."""

    name = "http"

    def __init__(self, url: str, *, timeout_s: float = 20.0) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self._request_id = 0
        self._client = httpx.Client(timeout=timeout_s)
        self.initialize()

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {},
        }
        try:
            response = self._client.post(self.url, json=request, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise McpClientError(f"MCP HTTP transport failed: {exc}") from exc
        if response.status_code >= 400:
            raise McpClientError(f"MCP HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if "error" in payload:
            raise McpClientError(f"{method} failed: {payload['error']}")
        return dict(payload["result"])

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "clientInfo": {"name": "researchpilot-agent"}},
        )

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self._rpc("tools/list").get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpMcpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_mcp_client(
    settings: Settings | None = None, *, transport: str | None = None, server: Any = None
) -> InProcessMcpClient | StdioMcpClient | HttpMcpClient:
    settings = settings or get_settings()
    mode = transport or settings.mcp_transport
    if mode == "inprocess":
        if server is None:
            from researchpilot.mcp.server import McpServer

            server = McpServer(settings)
        return InProcessMcpClient(server)
    if mode == "stdio":
        return StdioMcpClient(timeout_s=settings.mcp_timeout_s)
    if mode == "http":
        return HttpMcpClient(settings.mcp_url, timeout_s=settings.mcp_timeout_s)
    raise ValueError(f"unknown MCP transport: {mode}")
