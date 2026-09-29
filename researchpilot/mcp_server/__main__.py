"""``python -m researchpilot.mcp_server`` -> MCP server (stdio by default)."""

from __future__ import annotations

import sys

from researchpilot.mcp.server import main, serve_stdio

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        serve_stdio()
    else:
        main()
