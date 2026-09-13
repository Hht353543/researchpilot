"""Knowledge base facade: ingest, persist, search, inspect."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.observability.trace import Tracer
from researchpilot.rag.chunker import MarkdownChunker
from researchpilot.rag.embeddings import Embedder, build_embedder
from researchpilot.rag.loader import DocumentLoader
from researchpilot.rag.models import Chunk, Document
from researchpilot.rag.retriever import RetrievalResult, Retriever
from researchpilot.rag.vector_store import VectorStore
from researchpilot.utils import utc_now_iso

logger = logging.getLogger("researchpilot.rag")


class IngestReport(BaseModel):
    documents: int = 0
    chunks: int = 0
    characters_in: int = 0
    characters_out: int = 0
    skipped: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0


class KnowledgeBase:
    """Owns the vector store, embedder and retriever."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store: VectorStore | None = None,
        embedder: Embedder | None = None,
        chunker: MarkdownChunker | None = None,
        loader: DocumentLoader | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or build_embedder(self.settings)
        self.store = store or VectorStore(dimension=self.embedder.dimension)
        self.chunker = chunker or MarkdownChunker()
        self.loader = loader or DocumentLoader()
        self.store.fingerprint = self.embedder_fingerprint()

    def embedder_fingerprint(self) -> str:
        """Identity of the embedder a persisted index must match."""
        return (
            f"{self.settings.embedding_provider}:"
            f"{self.settings.embedding_model}:"
            f"{getattr(self.embedder, 'dimension', self.settings.embedding_dim)}"
        )

    # -- ingestion --------------------------------------------------------- #
    def ingest_path(self, path: str | Path) -> IngestReport:
        documents = self.loader.load_path(path)
        return self.ingest_documents(documents)

    def ingest_documents(self, documents: list[Document]) -> IngestReport:
        import time

        start = time.perf_counter()
        report = IngestReport()
        new_chunks: list[Chunk] = []
        for document in documents:
            if not document.content.strip():
                report.skipped.append(f"{document.doc_id}: empty content")
                continue
            chunks = self.chunker.chunk_document(document)
            if not chunks:
                report.skipped.append(f"{document.doc_id}: no chunk produced")
                continue
            self.store.upsert_document(document)
            report.documents += 1
            report.characters_in += len(document.content)
            report.characters_out += sum(len(c.content) for c in chunks)
            new_chunks.extend(chunks)
        if new_chunks:
            vectors = self.embedder.embed([c.content for c in new_chunks])
            self.store.add_chunks(
                [chunk.with_embedding(vector) for chunk, vector in zip(new_chunks, vectors, strict=True)]
            )
            report.chunks = len(new_chunks)
        report.duration_ms = round((time.perf_counter() - start) * 1000, 3)
        return report

    def ingest_text(
        self, content: str, *, title: str, source: str = "inline", metadata: dict[str, Any] | None = None
    ) -> IngestReport:
        from researchpilot.utils import sha1_of

        document = Document(
            doc_id=f"doc_{sha1_of(source + title)}",
            title=title,
            source=source,
            kind="knowledge_base",
            content=content,
            metadata=metadata or {},
            created_at=utc_now_iso(),
        )
        return self.ingest_documents([document])

    def load_default(self) -> IngestReport:
        """Full rebuild: drop the current index, then ingest the KB directory.

        A rebuild (not a merge) is what makes ``/kb/reindex`` correct when files
        were deleted or renamed on disk - otherwise stale chunks stay searchable.
        """
        self.store.clear()
        return self.ingest_path(self.settings.kb_dir())

    def delete_document(self, doc_id: str) -> int:
        """Remove a document and all of its chunks; returns the chunks removed."""
        removed = self.store.remove_document(doc_id)
        if removed:
            self.save()
        return removed

    # -- retrieval --------------------------------------------------------- #
    def retriever(
        self,
        *,
        tracer: Tracer | None = None,
        llm_runner: StructuredLLMRunner | None = None,
        rerank_strategy: str | None = None,
    ) -> Retriever:
        return Retriever(
            self.store,
            self.embedder,
            settings=self.settings,
            llm_runner=llm_runner,
            tracer=tracer,
            rerank_strategy=(rerank_strategy or self.settings.rerank_strategy),  # type: ignore[arg-type]
        )

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        strategy: str = "hybrid",
        filters: dict[str, Any] | None = None,
        tracer: Tracer | None = None,
        llm_runner: StructuredLLMRunner | None = None,
        rewrite: bool = True,
        rerank_strategy: str | None = None,
    ) -> RetrievalResult:
        # ``rerank_strategy`` lets the caller (agent/runtime) win over the settings
        # captured when this knowledge base was constructed.
        retriever = self.retriever(tracer=tracer, llm_runner=llm_runner, rerank_strategy=rerank_strategy)
        return retriever.retrieve(
            query,
            top_k=top_k,
            strategy=strategy,  # type: ignore[arg-type]
            filters=filters,
            rewrite=rewrite,
            max_context_chars=12_000,
        )

    # -- inspection -------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    def summaries(self) -> list[Any]:
        return self.store.summaries()

    def topics(self) -> list[str]:
        return self.store.topics()

    def get_document(self, doc_id: str) -> Document | None:
        return self.store.get_document(doc_id)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self.store.get_chunk(chunk_id)

    def document_chunks(self, doc_id: str) -> list[Chunk]:
        return [c for c in self.store.chunks() if c.doc_id == doc_id]

    # -- persistence ------------------------------------------------------- #
    def index_path(self) -> Path:
        return self.settings.runs_dir() / "knowledge_base.json"

    def save(self, path: str | Path | None = None) -> Path:
        self.store.fingerprint = self.embedder_fingerprint()
        return self.store.save(path or self.index_path())

    @classmethod
    def load_or_create(cls, settings: Settings | None = None) -> KnowledgeBase:
        settings = settings or get_settings()
        kb = cls(settings)
        path = kb.index_path()
        if path.exists():
            loaded = VectorStore.load(path)
            expected = kb.embedder_fingerprint()
            if loaded.fingerprint == expected:
                kb.store = loaded
                kb.store.dimension = kb.embedder.dimension
                return kb
            # A different embedder makes the stored vectors meaningless for queries
            # from this embedder: rebuild instead of silently returning garbage.
            logger.warning(
                "knowledge base index was built with %r but the configured embedder is %r; "
                "rebuilding the index",
                loaded.fingerprint or "unknown",
                expected,
            )
        kb.load_default()
        kb.save()
        return kb
