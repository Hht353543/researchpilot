"""document_reader: fetch a whole document (or a single chunk) from the knowledge base."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from researchpilot.tools.base import (
    BaseTool,
    ToolContext,
    ToolItem,
    ToolPermission,
    ToolRequestContext,
    ToolResult,
)
from researchpilot.utils import truncate


class DocumentReaderArgs(BaseModel):
    doc_id: str = Field(default="", max_length=200, description="document id")
    chunk_id: str = Field(default="", max_length=250, description="or a specific chunk id")
    max_chars: int = Field(default=6000, ge=200, le=40_000)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _need_target(self) -> DocumentReaderArgs:
        if not self.doc_id and not self.chunk_id:
            raise ValueError("either doc_id or chunk_id is required")
        return self


class DocumentReaderTool(BaseTool):
    name = "document_reader"
    description = (
        "Read a knowledge-base document or chunk by id. Use it to verify details, resolve "
        "ambiguity or quote exact wording after a search."
    )
    permission = ToolPermission.READ_ONLY
    timeout_s = 8.0
    max_retries = 1
    tags = ["document"]
    args_model = DocumentReaderArgs

    def build_arguments(self, request: ToolRequestContext) -> dict[str, Any] | None:
        """Only callable when the agent already knows which document to read."""
        if not request.doc_id_hint:
            return None
        return {"doc_id": request.doc_id_hint, "max_chars": 6000}

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, DocumentReaderArgs)
        kb = ctx.require("knowledge_base")
        if args.chunk_id:
            chunk = kb.get_chunk(args.chunk_id)
            if chunk is None:
                return ToolResult(tool=self.name, ok=False, error=f"chunk not found: {args.chunk_id}")
            content = chunk.content[args.offset : args.offset + args.max_chars]
            return ToolResult(
                tool=self.name,
                ok=True,
                items=[
                    ToolItem(
                        source_id=chunk.chunk_id,
                        content=content,
                        title=chunk.title,
                        kind="document",
                        doc_id=chunk.doc_id,
                        chunk_id=chunk.chunk_id,
                        metadata=dict(chunk.metadata),
                    )
                ],
                data={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "truncated": len(content) < len(chunk.content),
                },
            )
        document = kb.get_document(args.doc_id)
        if document is None:
            return ToolResult(tool=self.name, ok=False, error=f"document not found: {args.doc_id}")
        content = document.content[args.offset : args.offset + args.max_chars]
        return ToolResult(
            tool=self.name,
            ok=True,
            items=[
                ToolItem(
                    source_id=f"{document.doc_id}#full",
                    content=content,
                    title=document.title,
                    kind="document",
                    doc_id=document.doc_id,
                    metadata={
                        **document.metadata,
                        "source": document.source,
                        "truncated": len(content) < len(document.content),
                    },
                )
            ],
            data={
                "document": {
                    "doc_id": document.doc_id,
                    "title": document.title,
                    "source": document.source,
                    "content": truncate(content, 500),
                },
                "characters": len(content),
            },
        )
