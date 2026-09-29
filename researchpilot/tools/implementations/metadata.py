"""metadata: knowledge-base statistics, document metadata and current time."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.tools.base import (
    BaseTool,
    ToolContext,
    ToolPermission,
    ToolRequestContext,
    ToolResult,
)


class MetadataArgs(BaseModel):
    subject: Literal["documents", "document", "chunk", "topics", "stats", "time", "task"] = "stats"
    id: str = Field(default="", max_length=250)
    limit: int = Field(default=20, ge=1, le=100)


class MetadataTool(BaseTool):
    name = "metadata"
    description = (
        "Inspect knowledge-base metadata (documents, chunks, topics, stats), the current time, "
        "or the task context. Use it for coverage checks and recency framing."
    )
    permission = ToolPermission.READ_ONLY
    timeout_s = 5.0
    max_retries = 0
    tags = ["metadata"]
    args_model = MetadataArgs

    def build_arguments(self, request: ToolRequestContext) -> dict[str, Any] | None:
        """Coverage / metadata lookups default to knowledge-base statistics."""
        return {"subject": "stats", "limit": 20}

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MetadataArgs)
        kb = ctx.service("knowledge_base")
        data: dict[str, Any] = {"subject": args.subject}
        if args.subject == "time":
            now = time.time()
            data.update(
                {
                    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "epoch_s": int(now),
                    "timezone": time.strftime("%Z"),
                }
            )
        elif args.subject == "task":
            data["task"] = {
                "task_id": ctx.task_id,
                "services": sorted(ctx.services),
                **{k: v for k, v in ctx.scratch.items() if isinstance(v, str | int | float | bool)},
            }
        elif kb is None:
            return ToolResult(tool=self.name, ok=False, error="knowledge base service unavailable")
        elif args.subject == "stats":
            data.update(kb.stats())
        elif args.subject == "topics":
            data["topics"] = kb.topics()
        elif args.subject == "documents":
            data["documents"] = [s.model_dump() for s in kb.summaries()[: args.limit]]
        elif args.subject == "document":
            document = kb.get_document(args.id)
            if document is None:
                return ToolResult(tool=self.name, ok=False, error=f"document not found: {args.id}")
            data["document"] = {
                "doc_id": document.doc_id,
                "title": document.title,
                "source": document.source,
                "kind": document.kind,
                "created_at": document.created_at,
                "characters": len(document.content),
                "metadata": document.metadata,
                "chunks": len(kb.document_chunks(document.doc_id)),
            }
        elif args.subject == "chunk":
            chunk = kb.get_chunk(args.id)
            if chunk is None:
                return ToolResult(tool=self.name, ok=False, error=f"chunk not found: {args.id}")
            data["chunk"] = {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "characters": len(chunk.content),
                "metadata": {k: v for k, v in chunk.metadata.items() if k != "clean_stats"},
            }
        return ToolResult(tool=self.name, ok=True, data=data)
