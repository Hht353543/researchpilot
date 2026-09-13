"""MCP tool: the agent consumes MCP server capabilities through the same registry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from researchpilot.tools.base import BaseTool, ToolContext, ToolItem, ToolPermission, ToolResult

MCP_TOOL_NAME = "get_research_context"


class McpArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    subtask: str = Field(default="", max_length=300)
    top_k: int = Field(default=5, ge=1, le=20)


class McpResearchContextTool(BaseTool):
    name = "mcp_research_context"
    description = (
        "Call the ResearchPilot MCP server tool 'get_research_context' to obtain a packaged "
        "research context (knowledge-base hits + curated web references + coverage metadata)."
    )
    permission = ToolPermission.NETWORK
    timeout_s = 20.0
    max_retries = 1
    tags = ["mcp", "retrieval"]
    span_kind = "mcp"
    args_model = McpArgs

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, McpArgs)
        client = ctx.require("mcp_client")
        payload: dict[str, Any] = client.call_tool(
            MCP_TOOL_NAME,
            {"query": args.query, "subtask": args.subtask, "top_k": args.top_k},
        )
        if payload.get("isError"):
            return ToolResult(tool=self.name, ok=False, error=str(payload.get("error") or "MCP call failed"))
        structured = payload.get("structuredContent") or payload.get("structured_content") or {}
        items = [
            ToolItem(
                source_id=str(entry.get("source_id") or entry.get("chunk_id") or entry.get("url")),
                content=str(entry.get("content") or entry.get("snippet") or ""),
                title=str(entry.get("title") or ""),
                kind=str(entry.get("kind") or "mcp"),  # type: ignore[arg-type]
                doc_id=str(entry.get("doc_id") or ""),
                chunk_id=str(entry.get("chunk_id") or ""),
                url=str(entry.get("url") or ""),
                score=float(entry.get("score") or 0.0),
                metadata=dict(entry.get("metadata") or {}),
            )
            for entry in structured.get("items", [])
        ]
        return ToolResult(
            tool=self.name,
            ok=True,
            items=items,
            data={
                "server": structured.get("server", "researchpilot-mcp"),
                "transport": getattr(client, "name", "unknown"),
                "coverage": structured.get("coverage", {}),
                "hits": len(items),
            },
        )
