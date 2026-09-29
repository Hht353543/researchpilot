"""Knowledge-base data model: documents, chunks and retrieval hits."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DocKind = Literal["knowledge_base", "document", "web", "mcp"]


class Document(BaseModel):
    doc_id: str
    title: str
    source: str
    kind: DocKind = "knowledge_base"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class Chunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    source: str
    kind: DocKind = "knowledge_base"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    embedding: list[float] | None = None

    def with_embedding(self, vector: list[float]) -> Chunk:
        return self.model_copy(update={"embedding": vector})


class RetrievedChunk(BaseModel):
    chunk: Chunk
    score: float
    retriever: str
    rank: int = 0
    components: dict[str, float] = Field(default_factory=dict)

    @property
    def source_id(self) -> str:
        return self.chunk.chunk_id

    def as_item(self) -> dict[str, Any]:
        return {
            "source_id": self.chunk.chunk_id,
            "title": self.chunk.title,
            "content": self.chunk.content,
            "kind": self.chunk.kind,
            "doc_id": self.chunk.doc_id,
            "chunk_id": self.chunk.chunk_id,
            "score": round(self.score, 4),
            "retriever": self.retriever,
            "metadata": self.chunk.metadata,
        }


class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    source: str
    kind: DocKind = "knowledge_base"
    chunks: int = 0
    characters: int = 0
    created_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
