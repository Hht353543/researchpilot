"""Smoke-test an installed wheel without relying on the source checkout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from researchpilot import __version__
from researchpilot.api.app import create_app
from researchpilot.config import Settings
from researchpilot.mcp.server import McpServer
from researchpilot.schemas import ResearchRequest


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="researchpilot-wheel-smoke-") as directory:
        root = Path(directory)
        settings = Settings(
            provider="mock",
            kb_path=str(root / "knowledge-base"),
            runs_path=str(root / "runs"),
            embedding_dim=64,
            web_corpus_path=str(root / "web-corpus"),
        )
        cli_executable = Path(sys.executable).with_name(
            "researchpilot.exe" if os.name == "nt" else "researchpilot"
        )
        cli = subprocess.run(
            [str(cli_executable), "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if cli.returncode != 0 or "researchpilot" not in cli.stdout.lower():
            raise RuntimeError(f"installed CLI smoke failed: {cli.stderr[-500:]}")

        app = create_app(settings)
        result = app.state.container.run_research(
            ResearchRequest(question="Verify the installed package can run an offline research task.")
        )
        app.state.container.close()
        if result.status != "completed" or result.report is None:
            raise RuntimeError(f"installed offline research smoke failed: {result.status}")

        mcp = McpServer(settings)
        tool_names = sorted(mcp._tools)
        mcp.close()
        if "search_knowledge" not in tool_names:
            raise RuntimeError("installed MCP server did not register its core tools")

        print(
            json.dumps(
                {
                    "version": __version__,
                    "cli": "ok",
                    "config": "ok",
                    "api": "ok",
                    "mcp_tools": tool_names,
                    "research_status": result.status,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
