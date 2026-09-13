"""Convenience alias so ``python -m researchpilot.mcp_server`` works."""

from researchpilot.mcp.server import McpServer, create_http_app, main, serve_stdio

__all__ = ["McpServer", "create_http_app", "main", "serve_stdio"]
