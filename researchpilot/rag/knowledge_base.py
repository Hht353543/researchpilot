"""Knowledge base facade: ingest, persist, search, inspect."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.observability.trace import Tracer
from researchpilot.persistence import (
    PersistenceCorruptionError,
    PersistenceError,
    StorageSignature,
    serialized_file_update,
    storage_signature,
)
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
        self._owns_embedder = embedder is None
        self.embedder = embedder or build_embedder(self.settings)
        self.store = store or VectorStore(dimension=self.embedder.dimension)
        self._auto_refresh = store is None
        self.chunker = chunker or MarkdownChunker()
        self.loader = loader or DocumentLoader()
        self.store.fingerprint = self.embedder_fingerprint()
        self.persistence_error: str = ""
        self._lock = threading.RLock()
        self._dirty_document_ids: set[str] = set()
        self._deleted_document_ids: set[str] = set()
        self._replace_all = False
        self._rollback_store: VectorStore | None = None
        # A bare instance has not loaded the persisted index yet. load_or_create
        # sets this after loading; other instances refresh on their first read.
        self._storage_signature: StorageSignature | None = None

    def close(self) -> None:
        if self._owns_embedder:
            self.embedder.close()

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
        with self._lock:
            self._refresh_if_changed_unlocked()
            return self._ingest_documents_unlocked(documents)

    def _ingest_documents_unlocked(self, documents: list[Document]) -> IngestReport:
        import time

        start = time.perf_counter()
        report = IngestReport()
        pending: list[tuple[Document, list[Chunk]]] = []
        for document in documents:
            if not document.content.strip():
                report.skipped.append(f"{document.doc_id}: empty content")
                continue
            chunks = self.chunker.chunk_document(document)
            if not chunks:
                report.skipped.append(f"{document.doc_id}: no chunk produced")
                continue
            report.documents += 1
            report.characters_in += len(document.content)
            report.characters_out += sum(len(c.content) for c in chunks)
            pending.append((document, chunks))
        new_chunks = [chunk for _, chunks in pending for chunk in chunks]
        if new_chunks:
            vectors = self.embedder.embed([c.content for c in new_chunks])
            embedded = [
                chunk.with_embedding(vector) for chunk, vector in zip(new_chunks, vectors, strict=True)
            ]
            self._begin_mutation_unlocked()
            offset = 0
            for document, chunks in pending:
                replacement = embedded[offset : offset + len(chunks)]
                offset += len(chunks)
                # A document id identifies one current document version. Remove
                # every chunk from the previous version before inserting the new
                # ordered set, including stale tail chunks after a shrink.
                self.store.remove_document(document.doc_id)
                self.store.upsert_document(document)
                self.store.add_chunks(replacement)
                self._dirty_document_ids.add(document.doc_id)
                self._deleted_document_ids.discard(document.doc_id)
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
        with self._lock:
            self._begin_mutation_unlocked()
            self.store.clear()
            self._dirty_document_ids.clear()
            self._deleted_document_ids.clear()
            self._replace_all = True
            documents = self.loader.load_path(self.settings.kb_dir())
            return self._ingest_documents_unlocked(documents)

    def delete_document(self, doc_id: str) -> int:
        """Remove a document and all of its chunks; returns the chunks removed."""
        with self._lock:
            self._refresh_if_changed_unlocked()
            if self.store.get_document(doc_id) is None:
                return 0
            self._begin_mutation_unlocked()
            removed = self.store.remove_document(doc_id)
            if removed:
                self._dirty_document_ids.discard(doc_id)
                self._deleted_document_ids.add(doc_id)
                self.save()
            return removed

    def ingest_text_and_save(
        self,
        content: str,
        *,
        title: str,
        source: str = "inline",
        metadata: dict[str, Any] | None = None,
    ) -> IngestReport:
        """Apply and durably commit one inline document as a single operation."""
        with self._lock:
            report = self.ingest_text(
                content,
                title=title,
                source=source,
                metadata=metadata,
            )
            self.save()
            return report

    def ingest_path_and_save(self, path: str | Path) -> IngestReport:
        with self._lock:
            report = self.ingest_path(path)
            self.save()
            return report

    def reindex_and_save(self) -> IngestReport:
        with self._lock:
            report = self.load_default()
            self.save()
            return report

    # -- retrieval --------------------------------------------------------- #
    def retriever(
        self,
        *,
        tracer: Tracer | None = None,
        llm_runner: StructuredLLMRunner | None = None,
        rerank_strategy: str | None = None,
    ) -> Retriever:
        self._refresh_if_changed()
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
            # Honour the configured budget instead of a hardcoded 12k, so
            # RESEARCHPILOT_MAX_CONTEXT_CHARS actually controls context overflow.
            max_context_chars=self.settings.max_context_chars,
        )

    # -- inspection -------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        self._refresh_if_changed()
        stats = self.store.stats()
        if self.persistence_error:
            stats["persistence_error"] = self.persistence_error
        return stats

    def summaries(self) -> list[Any]:
        self._refresh_if_changed()
        return self.store.summaries()

    def topics(self) -> list[str]:
        self._refresh_if_changed()
        return self.store.topics()

    def get_document(self, doc_id: str) -> Document | None:
        self._refresh_if_changed()
        return self.store.get_document(doc_id)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        self._refresh_if_changed()
        return self.store.get_chunk(chunk_id)

    def document_chunks(self, doc_id: str) -> list[Chunk]:
        self._refresh_if_changed()
        return [c for c in self.store.chunks() if c.doc_id == doc_id]

    # -- persistence ------------------------------------------------------- #
    def index_path(self) -> Path:
        return self.settings.runs_dir() / "knowledge_base.json"

    def save(self, path: str | Path | None = None) -> Path:
        """Commit pending mutations, publishing them only after durable replacement."""
        target = Path(path) if path is not None else self.index_path()
        durable_before: VectorStore | None = None
        try:
            with self._lock, serialized_file_update(target):
                expected = self.embedder_fingerprint()
                latest: VectorStore | None = None
                if target.exists():
                    candidate = VectorStore.load(target)
                    if candidate.fingerprint == expected:
                        latest = candidate
                        durable_before = candidate.clone()

                committed = self.store.clone()
                if latest is not None and not self._replace_all:
                    for doc_id in self._deleted_document_ids:
                        latest.remove_document(doc_id)
                    for doc_id in self._dirty_document_ids:
                        latest.remove_document(doc_id)
                        document = self.store.get_document(doc_id)
                        if document is not None:
                            latest.upsert_document(document)
                            latest.add_chunks(
                                chunk for chunk in self.store.chunks() if chunk.doc_id == doc_id
                            )
                    committed = latest

                disk_generation = latest.generation if latest is not None else 0
                committed.fingerprint = expected
                committed.generation = max(committed.generation, disk_generation) + 1
                saved = committed.save(target)
                self.store = committed
                self._storage_signature = storage_signature(target)
                self._dirty_document_ids.clear()
                self._deleted_document_ids.clear()
                self._replace_all = False
                self._rollback_store = None
                self.persistence_error = ""
                return saved
        except Exception as exc:
            with self._lock:
                if durable_before is not None:
                    self.store = durable_before
                elif self._rollback_store is not None:
                    self.store = self._rollback_store
                self._dirty_document_ids.clear()
                self._deleted_document_ids.clear()
                self._replace_all = False
                self._rollback_store = None
                self._storage_signature = storage_signature(target)
                self.persistence_error = (
                    "corrupted" if isinstance(exc, PersistenceCorruptionError) else "write_failed"
                )
            logger.exception("could not persist the knowledge base index")
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError("knowledge base update could not be committed") from exc

    def _begin_mutation_unlocked(self) -> None:
        if self._rollback_store is None:
            self._rollback_store = self.store.clone()

    def _refresh_if_changed(self) -> None:
        with self._lock:
            self._refresh_if_changed_unlocked()

    def _refresh_if_changed_unlocked(self) -> None:
        """Reload a peer's atomic commit before serving the next read."""
        if self.persistence_error == "corrupted":
            raise PersistenceCorruptionError("knowledge base index is corrupted")
        if not self._auto_refresh:
            return
        if self._dirty_document_ids or self._deleted_document_ids or self._replace_all:
            return
        path = self.index_path()
        signature = storage_signature(path)
        if signature is None or signature == self._storage_signature:
            return
        loaded = VectorStore.load(path)
        if loaded.fingerprint != self.embedder_fingerprint():
            return
        loaded.dimension = self.embedder.dimension
        self.store = loaded
        self._storage_signature = signature
        self.persistence_error = ""

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
                kb._storage_signature = storage_signature(path)
                return kb
            # Vectors from another embedder are meaningless for this one, so rebuild.
            logger.warning(
                "knowledge base index was built with %r but the configured embedder is %r; "
                "rebuilding the index",
                loaded.fingerprint or "unknown",
                expected,
            )
        kb.load_default()
        kb.save()
        return kb
