"""MCP clients: in-process, stdio (subprocess) and HTTP."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from contextlib import suppress
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

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        timeout_s: float = 20.0,
        terminate_grace_s: float = 3.0,
    ) -> None:
        # A child process inherits the OS locale for its pipes (cp936 here), which
        # corrupts Chinese arguments in both directions, so pass UTF-8 explicitly.
        # The server pins its own streams as well; either side alone is enough.
        command = command or [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "researchpilot.mcp_server",
            "--stdio",
        ]
        self.timeout_s = timeout_s
        self.terminate_grace_s = terminate_grace_s
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
        self._responses: queue.Queue[str | bytes | None] = queue.Queue()
        self._rpc_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._reader_error: Exception | None = None
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._reader = threading.Thread(
            target=self._read_responses,
            name=f"mcp-stdio-reader-{getattr(self._process, 'pid', 'test')}",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name=f"mcp-stdio-stderr-{getattr(self._process, 'pid', 'test')}",
            daemon=True,
        )
        self._stderr_reader.start()
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise

    def _read_responses(self) -> None:
        try:
            if self._process.stdout is None:
                return
            for line in self._process.stdout:
                self._responses.put(line)
        except Exception as exc:  # closing a pipe wakes the reader on some platforms
            self._reader_error = exc
        finally:
            self._responses.put(None)

    def _read_stderr(self) -> None:
        try:
            if self._process.stderr is None:
                return
            for line in self._process.stderr:
                self._stderr_lines.append(str(line).rstrip())
        except (OSError, ValueError):
            return

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._rpc_lock:
            if self._closed or self._process.stdin is None:
                raise McpClientError("stdio transport is closed")
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            }
            try:
                self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
                line = self._responses.get(timeout=self.timeout_s)
            except queue.Empty as exc:
                self.close()
                raise McpClientError(f"MCP stdio call timed out after {self.timeout_s}s") from exc
            except (OSError, ValueError) as exc:
                self.close()
                raise McpClientError("MCP stdio transport write failed") from exc
            if line is None:
                returncode = self._process.poll()
                detail = f" (exit {returncode})" if returncode is not None else ""
                raise McpClientError(f"MCP server closed the stream{detail}")
            try:
                response = json.loads(line)
                if "error" in response:
                    raise McpClientError(f"{method} failed: {response['error']}")
                return dict(response["result"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise McpClientError("MCP server returned an invalid stdio response") from exc

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
        # Do not take ``_rpc_lock`` here: another thread may be blocked waiting
        # for stdout while application shutdown needs to terminate the child and
        # wake that wait immediately.
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._process.stdin is not None:
                with suppress(OSError, ValueError):
                    self._process.stdin.close()
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=self.terminate_grace_s)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    with suppress(subprocess.TimeoutExpired):
                        self._process.wait(timeout=self.terminate_grace_s)
            for stream in (self._process.stdout, self._process.stderr):
                if stream is not None:
                    with suppress(OSError, ValueError):
                        stream.close()
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=self.terminate_grace_s)
        if self._stderr_reader is not threading.current_thread():
            self._stderr_reader.join(timeout=self.terminate_grace_s)

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
