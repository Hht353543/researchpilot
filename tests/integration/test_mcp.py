"""MCP server/client integration: protocol surface, stdio transport, HTTP transport."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from researchpilot.config import Settings
from researchpilot.mcp.client import InProcessMcpClient, McpClientError, StdioMcpClient
from researchpilot.mcp.protocol import MCP_PROTOCOL_VERSION
from researchpilot.mcp.server import McpServer, create_http_app, serve_stdio
from researchpilot.rag.knowledge_base import KnowledgeBase


@pytest.fixture
def server(settings: Settings, knowledge_base: KnowledgeBase) -> McpServer:
    return McpServer(settings, knowledge_base=knowledge_base)


def test_initialize_and_tools_list(server: McpServer) -> None:
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init and init["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert init["result"]["serverInfo"]["name"] == "researchpilot-mcp"

    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert tools
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert {"search_knowledge", "get_document", "search_web", "get_research_context"} <= names
    for tool in tools["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_unknown_method_and_tool_errors(server: McpServer) -> None:
    unknown = server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert unknown and unknown["error"]["code"] == -32601

    bad_tool = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ghost", "arguments": {}}}
    )
    assert bad_tool and bad_tool["error"]["code"] == -32602

    bad_args = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "x"}},
        }
    )
    assert bad_args and bad_args["error"]["code"] == -32602


def test_tool_call_returns_structured_content(server: McpServer) -> None:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {"query": "混合检索 RRF", "top_k": 2}},
        }
    )
    assert response
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["items"]
    assert result["content"][0]["type"] == "text"
    assert result["_meta"]["latency_ms"] >= 0

    context = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "get_research_context", "arguments": {"query": "工具权限", "top_k": 2}},
        }
    )
    assert context
    structured = context["result"]["structuredContent"]
    assert structured["items"]
    assert structured["coverage"]["knowledge_base_documents"] >= 1


def test_get_document_tool(server: McpServer, knowledge_base: KnowledgeBase) -> None:
    doc_id = knowledge_base.summaries()[0].doc_id
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "get_document", "arguments": {"doc_id": doc_id, "max_chars": 200}},
        }
    )
    assert response
    assert response["result"]["structuredContent"]["items"]


def test_stdio_transport_roundtrip(server: McpServer) -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_web", "arguments": {"query": "MCP gateway", "top_k": 2}},
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert len(lines) == 3
    assert lines[0]["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert lines[2]["result"]["structuredContent"]["items"]


def test_stdio_transport_reports_parse_errors(server: McpServer) -> None:
    stdin = io.StringIO("{not json\n")
    stdout = io.StringIO()
    serve_stdio(server, stdin=stdin, stdout=stdout)
    payload = json.loads(stdout.getvalue().strip())
    assert payload["error"]["code"] == -32700


@pytest.mark.anyio
async def test_mcp_http_lifespan_closes_server(server: McpServer, monkeypatch) -> None:
    closed = False
    original_close = server.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    monkeypatch.setattr(server, "close", close)
    app = create_http_app(server)
    async with app.router.lifespan_context(app):
        assert not closed
    assert closed


def test_in_process_client(server: McpServer) -> None:
    client = InProcessMcpClient(server)
    assert client.name == "inprocess"
    assert client.list_tools()
    result = client.call_tool("search_knowledge", {"query": "记忆分层", "top_k": 2})
    assert result["structuredContent"]["items"]
    with pytest.raises(McpClientError):
        client.call_tool("does_not_exist", {})


def test_stdio_subprocess_client(settings: Settings, tmp_path, monkeypatch) -> None:
    import os

    monkeypatch.setenv("RESEARCHPILOT_KB_PATH", str(settings.kb_path))
    monkeypatch.setenv("RESEARCHPILOT_RUNS_PATH", str(tmp_path / "runs"))
    monkeypatch.setenv("RESEARCHPILOT_PROVIDER", "mock")
    client = StdioMcpClient(timeout_s=30)
    try:
        tools = client.list_tools()
        assert {tool["name"] for tool in tools} >= {"search_knowledge", "get_research_context"}
        result = client.call_tool("search_knowledge", {"query": "工具注册表", "top_k": 2})
        assert result["structuredContent"]["items"]
    finally:
        client.close()
        assert os.environ.get("RESEARCHPILOT_KB_PATH") == str(settings.kb_path)


@pytest.mark.anyio
async def test_http_transport(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    app = create_http_app(McpServer(settings, knowledge_base=knowledge_base))
    # Regression: /openapi.json used to raise PydanticUserError because the
    # response annotation was imported inside create_http_app.
    assert set(app.openapi()["paths"]) >= {"/health", "/mcp"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert "search_knowledge" in health.json()["tools"]

        init = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert init.json()["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION

        call = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": {"query": "重排", "top_k": 2}},
            },
        )
        assert call.json()["result"]["structuredContent"]["items"]

        batch = await client.post(
            "/mcp",
            json=[
                {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            ],
        )
        assert len(batch.json()) == 2


@pytest.mark.anyio
async def test_http_health_reports_corrupted_knowledge_storage(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    app = create_http_app(McpServer(settings, knowledge_base=knowledge_base))
    knowledge_base.index_path().write_text('{"documents":', encoding="utf-8")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "server": "researchpilot-mcp",
        "storage": "unavailable",
    }


def test_mcp_tool_reports_corrupted_knowledge_storage(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    server = McpServer(settings, knowledge_base=knowledge_base)
    knowledge_base.index_path().write_text('{"documents":', encoding="utf-8")

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_knowledge",
                "arguments": {"query": "corrupted storage", "top_k": 2},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    payload = response["result"]["structuredContent"]
    assert payload["tool"] == "search_knowledge"
    assert "corrupted" in payload["error"].lower()
    assert "knowledge_base.json" not in payload["error"]


@pytest.mark.anyio
async def test_api_container_uses_http_mcp_server_process(tmp_path, monkeypatch) -> None:
    """The docker-compose topology: API -> HTTP MCP server -> tool -> result."""
    import os
    import socket
    import subprocess
    import sys
    import time

    import httpx as httpx_client

    from researchpilot.api.app import create_app
    from researchpilot.config import Settings as AppSettings

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    env = {
        **os.environ,
        "RESEARCHPILOT_MCP_TRANSPORT": "http",
        "RESEARCHPILOT_MCP_HOST": "127.0.0.1",
        "RESEARCHPILOT_MCP_PORT": str(port),
        "RESEARCHPILOT_KB_PATH": str(Path(__file__).resolve().parents[1] / "fixtures" / "kb"),
        "RESEARCHPILOT_RUNS_PATH": str(tmp_path / "mcp-runs"),
        "RESEARCHPILOT_PROVIDER": "mock",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "researchpilot.mcp_server"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline and process.poll() is None:
            try:
                if httpx_client.get(f"{base}/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        assert ready, "MCP HTTP server did not become healthy"

        settings = AppSettings(
            provider="mock",
            mcp_transport="http",
            mcp_url=f"{base}/mcp",
            mcp_timeout_s=20,
            kb_path=str(Path(__file__).resolve().parents[1] / "fixtures" / "kb"),
            runs_path=str(tmp_path / "runs"),
            embedding_dim=64,
            top_k=4,
            max_iterations=1,
        )
        app = create_app(settings)
        assert app.state.container.mcp_mode == "http"

        transport = httpx_client.ASGITransport(app=app)
        async with httpx_client.AsyncClient(
            transport=transport, base_url="http://api", timeout=120
        ) as client:
            tools = (await client.get("/mcp/tools")).json()
            assert tools["transport"] == "http"
            health = (await client.get("/health")).json()
            assert health["mcp_transport"] == "http"

            result = (
                await client.post(
                    "/research",
                    json={"question": "如何通过 MCP 网关把知识库复用到多个 Agent？"},
                )
            ).json()
            assert result["status"] == "completed"
            assert result["quality"] in {"succeeded", "degraded"}
            assert result["metrics"]["mcp_calls"] >= 1
            assert (
                any(item["tool"] == "mcp_research_context" for item in result["evidence"]["evidence"])
                or result["metrics"]["mcp_calls"] >= 1
            )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:  # pragma: no cover
            process.kill()
