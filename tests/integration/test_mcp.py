"""MCP server/client integration: protocol surface, stdio transport, HTTP transport."""

from __future__ import annotations

import io
import json

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
