"""knowledge_search: hybrid RAG retrieval over the internal knowledge base."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.tools.base import BaseTool, ToolContext, ToolItem, ToolPermission, ToolResult


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=500, description="retrieval query")
    top_k: int = Field(default=5, ge=1, le=20)
    strategy: Literal["dense", "keyword", "hybrid"] = "hybrid"
    rewrite: bool = Field(default=True, description="rewrite the query before retrieval")
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="metadata filters, e.g. {'topic': 'rag'} or {'tags': ['security']}",
    )


class KnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = (
        "Search the internal knowledge base with hybrid (dense + BM25) retrieval, optional "
        "query rewriting, metadata filters and reranking. Returns quotable passages."
    )
    permission = ToolPermission.READ_ONLY
    timeout_s = 15.0
    max_retries = 1
    tags = ["rag", "retrieval"]
    args_model = KnowledgeSearchArgs

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, KnowledgeSearchArgs)
        kb = ctx.require("knowledge_base")
        result = kb.search(
            args.query,
            top_k=args.top_k,
            strategy=args.strategy,
            filters=args.filters or None,
            tracer=ctx.tracer,
            llm_runner=ctx.service("llm_runner"),
            rewrite=args.rewrite,
        )
        items = [
            ToolItem(
                source_id=hit.chunk.chunk_id,
                content=hit.chunk.content,
                title=hit.chunk.title,
                kind="knowledge_base",
                doc_id=hit.chunk.doc_id,
                chunk_id=hit.chunk.chunk_id,
                score=hit.score,
                metadata={
                    **{k: v for k, v in hit.chunk.metadata.items() if k != "clean_stats"},
                    "retriever": hit.retriever,
                    "section": hit.chunk.metadata.get("section", ""),
                },
            )
            for hit in result.hits
        ]
        return ToolResult(
            tool=self.name,
            ok=True,
            items=items,
            data={
                "query": result.query,
                "rewritten_query": result.rewritten_query,
                "strategy": result.strategy,
                "candidates": result.candidates,
                "hits": len(items),
            },
        )
