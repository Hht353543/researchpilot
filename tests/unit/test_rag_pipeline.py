"""Unit tests for loader -> cleaner -> chunker -> embeddings -> vector store -> retriever."""

from __future__ import annotations

from pathlib import Path

import pytest

from researchpilot.config import Settings
from researchpilot.rag.chunker import MarkdownChunker
from researchpilot.rag.cleaner import TextCleaner
from researchpilot.rag.embeddings import HashEmbedder, build_embedder
from researchpilot.rag.loader import DocumentLoader
from researchpilot.rag.models import Chunk, Document
from researchpilot.rag.retriever import Retriever
from researchpilot.rag.vector_store import VectorStore, tokenize

FIXTURE_KB = Path(__file__).resolve().parents[1] / "fixtures" / "kb"


def test_knowledge_base_closes_only_the_embedder_it_owns(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researchpilot.rag.embeddings import Embedder
    from researchpilot.rag.knowledge_base import KnowledgeBase

    class CloseTrackingEmbedder(Embedder):
        name = "tracking"
        dimension = 8

        def __init__(self) -> None:
            self.close_calls = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * self.dimension for _ in texts]

        def close(self) -> None:
            self.close_calls += 1

    owned_embedder = CloseTrackingEmbedder()
    monkeypatch.setattr("researchpilot.rag.knowledge_base.build_embedder", lambda _settings: owned_embedder)
    owned = KnowledgeBase(settings)
    owned.close()
    assert owned_embedder.close_calls == 1

    shared_embedder = CloseTrackingEmbedder()
    shared = KnowledgeBase(settings, embedder=shared_embedder)
    shared.close()
    assert shared_embedder.close_calls == 0


class _ParagraphChunker:
    """Predictable chunker for document-replacement contract tests."""

    def chunk_document(self, document: Document) -> list[Chunk]:
        return [
            Chunk(
                chunk_id=f"{document.doc_id}#c{position:03d}",
                doc_id=document.doc_id,
                title=document.title,
                source=document.source,
                content=content.strip(),
                metadata={"position": position},
            )
            for position, content in enumerate(document.content.split("|"))
            if content.strip()
        ]


def _document(doc_id: str, content: str) -> Document:
    return Document(doc_id=doc_id, title=doc_id, source=f"test://{doc_id}", content=content)


def test_reingest_replaces_single_chunk_without_touching_other_documents(settings: Settings) -> None:
    from researchpilot.rag.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(settings, embedder=HashEmbedder(32), chunker=_ParagraphChunker())
    kb.ingest_documents([_document("target", "old"), _document("other", "keep")])

    kb.ingest_documents([_document("target", "new")])

    assert [chunk.content for chunk in kb.document_chunks("target")] == ["new"]
    assert [chunk.content for chunk in kb.document_chunks("other")] == ["keep"]


def test_reingest_replaces_middle_chunk_and_preserves_order(settings: Settings) -> None:
    from researchpilot.rag.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(settings, embedder=HashEmbedder(32), chunker=_ParagraphChunker())
    kb.ingest_documents([_document("target", "first|old middle|last")])

    kb.ingest_documents([_document("target", "first|new middle|last")])

    chunks = kb.document_chunks("target")
    assert [chunk.chunk_id for chunk in chunks] == [
        "target#c000",
        "target#c001",
        "target#c002",
    ]
    assert [chunk.content for chunk in chunks] == ["first", "new middle", "last"]


def test_reingest_removes_stale_chunks_when_chunk_count_changes(settings: Settings) -> None:
    from researchpilot.rag.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(settings, embedder=HashEmbedder(32), chunker=_ParagraphChunker())
    kb.ingest_documents([_document("target", "one|two|three|four")])
    kb.ingest_documents([_document("target", "replacement")])
    assert [chunk.content for chunk in kb.document_chunks("target")] == ["replacement"]

    kb.ingest_documents([_document("target", "one|two|three")])
    assert [chunk.content for chunk in kb.document_chunks("target")] == ["one", "two", "three"]


def test_loader_reads_markdown_with_front_matter() -> None:
    documents = DocumentLoader().load_path(FIXTURE_KB)
    assert len(documents) == 5
    first = next(doc for doc in documents if doc.doc_id == "kb-fixture-tools")
    assert first.title == "工具与权限"
    assert first.metadata["topic"] == "tools"
    assert first.created_at.startswith("2026-01-02")


def test_cleaner_removes_boilerplate_and_dedupes_lines() -> None:
    cleaner = TextCleaner()
    repeated = "正文第一段内容比较长需要保留下来，重复行应当被去重处理。"
    text = f"版权所有 © 示例\n\n{repeated}\n{repeated}\n\n\n结束。"
    cleaned, stats = cleaner.clean(text)
    assert "版权所有" not in cleaned
    assert cleaned.count("正文第一段内容比较长需要保留下来") == 1
    assert stats["duplicate_lines_removed"] == 1
    assert stats["chars_out"] < stats["chars_in"]


def test_chunker_is_heading_aware_and_overlaps_by_sentence() -> None:
    document = Document(
        doc_id="d1",
        title="T",
        source="s",
        content="# 标题一\n\n第一段内容足够长以便成为独立分块。第二句用于测试。\n\n## 标题二\n\n第二段内容也很长，用于验证分块上下文。",
    )
    chunker = MarkdownChunker(target_chars=40, max_chars=60, overlap_chars=30, min_chars=10)
    chunks = chunker.chunk_document(document)
    assert chunks
    assert all(chunk.metadata["section"] for chunk in chunks)
    assert chunks[0].metadata["section"].startswith("标题一")
    assert any("标题二" in chunk.metadata["section"] for chunk in chunks)
    for chunk in chunks:
        assert chunk.content.strip().startswith(chunk.metadata["section"])
        assert len(chunk.content) <= 60 + 40


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    embedder = HashEmbedder(64)
    first = embedder.embed_one("MCP 基于 JSON-RPC 2.0")
    second = embedder.embed_one("MCP 基于 JSON-RPC 2.0")
    assert first == second
    assert len(first) == 64
    norm = sum(v * v for v in first) ** 0.5
    assert 0.99 <= norm <= 1.01
    assert embedder.embed_one("完全不同的内容 ABC") != first


def test_build_embedder_defaults_to_hash(settings: Settings) -> None:
    embedder = build_embedder(settings)
    assert embedder.name == "hash"
    assert embedder.dimension == settings.embedding_dim


def test_tokenize_keeps_cjk_words_and_latin_tokens() -> None:
    tokens = tokenize("MCP 基于 JSON-RPC 2.0 的 tools/call 方法")
    assert "mcp" in tokens
    assert any("工具" in t or "方法" in t for t in tokens)


def _store() -> VectorStore:
    embedder = HashEmbedder(128)
    store = VectorStore(dimension=128)
    chunks = [
        Chunk(
            chunk_id=f"c{i}",
            doc_id=f"doc{i}",
            title=f"标题{i}",
            source=f"src{i}",
            content=content,
            metadata={"topic": topic},
        )
        for i, (content, topic) in enumerate(
            [
                ("混合检索通过 RRF 融合语义与关键词排名。", "rag"),
                ("工具注册表声明权限、超时与重试策略。", "tools"),
                ("长期记忆需要 TTL 与去重策略。", "memory"),
            ]
        )
    ]
    store.add_chunks(
        [
            c.with_embedding(v)
            for c, v in zip(chunks, embedder.embed([c.content for c in chunks]), strict=True)
        ]
    )
    return store


def test_vector_store_dense_keyword_and_hybrid_search() -> None:
    embedder = HashEmbedder(128)
    store = _store()
    query = "RRF 混合检索"
    vector = embedder.embed_one(query)
    dense = store.dense_search(vector, k=3)
    keyword = store.keyword_search(query, k=3)
    hybrid = store.hybrid_search(query, vector, k=3)
    assert dense and keyword and hybrid
    assert hybrid[0].chunk.chunk_id in {hit.chunk.chunk_id for hit in dense + keyword}
    assert hybrid[0].retriever == "hybrid"
    assert hybrid[0].rank == 1


def test_vector_store_metadata_filtering() -> None:
    store = _store()
    vector = HashEmbedder(128).embed_one("检索")
    hits = store.dense_search(vector, k=5, filters={"topic": "tools"})
    assert hits
    assert {hit.chunk.metadata["topic"] for hit in hits} == {"tools"}
    assert store.dense_search(vector, k=5, filters={"topic": "missing"}) == []


def test_retriever_rewrites_reranks_and_builds_context() -> None:
    embedder = HashEmbedder(128)
    store = _store()
    retriever = Retriever(store, embedder, settings=Settings(embedding_dim=128, top_k=2))
    result = retriever.retrieve("工具注册表需要声明什么", top_k=2, rewrite=True, rerank=True)
    assert result.strategy == "hybrid"
    assert result.hits
    assert result.hits[0].chunk.chunk_id == "c1"
    assert result.rerank_strategy == "heuristic"
    assert "[" in result.context  # context is labelled with chunk ids
    assert result.items()[0]["source_id"] == "c1"


def test_retriever_traces_spans() -> None:
    from researchpilot.observability.trace import Tracer, tracer_scope

    embedder = HashEmbedder(64)
    store = _store()
    tracer = Tracer("task-trace", "工具注册表")
    retriever = Retriever(store, embedder, settings=Settings(embedding_dim=64, top_k=2), tracer=tracer)
    with tracer_scope(tracer):
        retriever.retrieve("工具注册表", top_k=1)
    kinds = [span.kind for span in tracer.spans]
    assert "retrieval" in kinds
    assert any(span.output.get("doc_ids") for span in tracer.spans if span.kind == "retrieval")


def test_vector_store_roundtrip(tmp_path: Path) -> None:
    store = _store()
    path = store.save(tmp_path / "vs.json")
    loaded = VectorStore.load(path)
    assert loaded.stats()["chunks"] == store.stats()["chunks"]
    assert loaded.stats()["documents"] == store.stats()["documents"]


def test_embedder_change_invalidates_persisted_index(tmp_path: Path, settings) -> None:
    """Regression: switching embedders must rebuild, never reuse stale vectors."""
    from researchpilot.rag.knowledge_base import KnowledgeBase

    base = settings.model_copy(update={"runs_path": str(tmp_path / "runs")})
    kb = KnowledgeBase(base)
    kb.ingest_text("混合检索通过 RRF 融合语义与关键词排名。" * 4, title="doc")
    kb.save()
    first = KnowledgeBase.load_or_create(base)
    assert first.store.stats()["chunks"] == kb.store.stats()["chunks"]
    assert first.store.fingerprint == kb.embedder_fingerprint()

    # Same corpus, different embedder configuration -> index must be rebuilt.
    switched = base.model_copy(update={"embedding_dim": 96})
    rebuilt = KnowledgeBase.load_or_create(switched)
    assert rebuilt.embedder_fingerprint() != first.embedder_fingerprint()
    assert rebuilt.store.fingerprint == rebuilt.embedder_fingerprint()
    assert rebuilt.store.dimension == 96
    assert rebuilt.store.stats()["chunks"] >= 1


@pytest.mark.parametrize(
    "text",
    ["", "   ", "短文本"],
)
def test_chunker_handles_edge_cases(text: str) -> None:
    document = Document(doc_id="d", title="t", source="s", content=text)
    chunks = MarkdownChunker(min_chars=1).chunk_document(document)
    if text.strip():
        assert chunks
    else:
        assert chunks == []
