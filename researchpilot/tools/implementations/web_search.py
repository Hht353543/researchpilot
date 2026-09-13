"""web_search: pluggable web backend (bundled offline corpus or HTTP API)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from researchpilot.tools.base import BaseTool, ToolContext, ToolItem, ToolPermission, ToolResult


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    top_k: int = Field(default=5, ge=1, le=20)
    recency_days: int | None = Field(
        default=None, ge=1, le=3650, description="only keep results newer than N days"
    )


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the web (or the bundled offline web corpus in offline mode) for market, "
        "trend and recency-sensitive facts. Returns titles, urls, dates and snippets."
    )
    permission = ToolPermission.NETWORK
    timeout_s = 12.0
    max_retries = 1
    tags = ["web", "search"]
    args_model = WebSearchArgs

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WebSearchArgs)
        backend = ctx.require("web_backend")
        span = (
            ctx.tracer.start_span(
                "web_search.retrieval",
                "retrieval",
                tool=self.name,
                input={"query": args.query, "top_k": args.top_k},
            )
            if ctx.tracer is not None
            else None
        )
        results: list[dict[str, Any]] = backend.search(
            args.query, top_k=args.top_k, recency_days=args.recency_days
        )
        if span is not None and ctx.tracer is not None:
            doc_ids: list[str] = []
            for result in results:
                doc_id = str(result.get("doc_id") or result.get("source_id") or "")
                if doc_id and doc_id not in doc_ids:
                    doc_ids.append(doc_id)
            ctx.tracer.end_span(
                span,
                output={
                    "doc_ids": doc_ids,
                    "hits": [{"source_id": r.get("source_id"), "score": r.get("score")} for r in results],
                },
                metadata={"backend": getattr(backend, "name", "unknown")},
            )
        items = [
            ToolItem(
                source_id=str(r.get("source_id") or r.get("id") or r.get("url")),
                content=str(r.get("content") or r.get("snippet") or ""),
                title=str(r.get("title") or ""),
                kind="web",
                url=str(r.get("url") or ""),
                score=float(r.get("score") or 0.0),
                metadata={
                    "synthetic": bool(r.get("synthetic", False)),
                    "date": r.get("date", ""),
                    "publisher": r.get("publisher", ""),
                },
            )
            for r in results
        ]
        return ToolResult(
            tool=self.name,
            ok=True,
            items=items,
            data={
                "backend": getattr(backend, "name", "unknown"),
                "query": args.query,
                "results": len(items),
            },
        )
