"""MCP (Model Context Protocol) server + clients.

Implements the protocol surface the platform needs (initialize / tools/list /
tools/call) with three transports: in-process, stdio and streamable HTTP.
"""

from researchpilot.mcp.client import (
    HttpMcpClient,
    InProcessMcpClient,
    McpClientError,
    StdioMcpClient,
    build_mcp_client,
)
from researchpilot.mcp.protocol import MCP_PROTOCOL_VERSION, JsonRpcError, McpToolSpec
from researchpilot.mcp.server import McpServer, create_http_app, main

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "HttpMcpClient",
    "InProcessMcpClient",
    "JsonRpcError",
    "McpClientError",
    "McpServer",
    "McpToolSpec",
    "StdioMcpClient",
    "build_mcp_client",
    "create_http_app",
    "main",
]
