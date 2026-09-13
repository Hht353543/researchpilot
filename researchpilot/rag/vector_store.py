"""Vector store with dense, keyword (BM25) and hybrid (RRF) retrieval."""

from __future__ import annotations

import itertools
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from researchpilot.rag.models import Chunk, Document, DocumentSummary, RetrievedChunk
from researchpilot.utils import cosine_similarity, normalize_text

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """Tokenise for BM25: latin words + CJK unigrams/bigrams (jieba when present)."""
    lowered = normalize_text(text)
    tokens = [t.lower() for t in _WORD_RE.findall(lowered)]
    try:  # pragma: no cover - optional dependency
        import jieba

        tokens.extend(t.strip() for t in jieba.lcut(lowered) if t.strip() and not t.isspace())
    except Exception:
        pass
    cjk = _CJK_RE.findall(lowered)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in itertools.pairwise(cjk))
    return [t for t in tokens if t]


class BM25Index:
    """Okapi BM25 over the chunk corpus (rebuilt on every mutation)."""

    def __init__(self, documents: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.documents = documents
        self.doc_len = [len(d) for d in documents]
        self.avgdl = (sum(self.doc_len) / len(documents)) if documents else 0.0
        self.term_freqs: list[Counter[str]] = [Counter(d) for d in documents]
        df: Counter[str] = Counter()
        for freq in self.term_freqs:
            df.update(freq.keys())
        total = len(documents)
        self.idf = {term: math.log(1 + (total - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def scores(self, query_tokens: Iterable[str]) -> dict[int, float]:
        query = [t for t in query_tokens if t in self.idf]
        results: dict[int, float] = {}
        if not query or not self.documents:
            return results
        for idx, freq in enumerate(self.term_freqs):
            length = self.doc_len[idx] or 1
            score = 0.0
            for term in query:
                tf = freq.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * length / max(self.avgdl, 1e-9))
                score += self.idf[term] * (tf * (self.k1 + 1)) / denom
            if score > 0:
                results[idx] = round(score, 6)
        return results


class VectorStore:
    """Chunk store with metadata filtering, dense/keyword/hybrid search + JSON persistence."""

    def __init__(self, *, dimension: int = 384) -> None:
        self.dimension = dimension
        self._chunks: dict[str, Chunk] = {}
        self._documents: dict[str, Document] = {}
        self._bm25: BM25Index | None = None
        self._idf: dict[str, float] | None = None
        self._bm25_ids: list[str] = []

    # -- mutations --------------------------------------------------------- #
    def upsert_document(self, document: Document) -> None:
        self._documents[document.doc_id] = document

    def add_chunks(self, chunks: Iterable[Chunk]) -> int:
        added = 0
        for chunk in chunks:
            if not chunk.embedding:
                raise ValueError(f"chunk {chunk.chunk_id} has no embedding")
            self._chunks[chunk.chunk_id] = chunk
            added += 1
        self._invalidate_index()
        return added

    def remove_document(self, doc_id: str) -> int:
        removed = [cid for cid, chunk in self._chunks.items() if chunk.doc_id == doc_id]
        for cid in removed:
            del self._chunks[cid]
        self._documents.pop(doc_id, None)
        self._invalidate_index()
        return len(removed)

    def clear(self) -> None:
        self._chunks.clear()
        self._documents.clear()
        self._invalidate_index()

    def _invalidate_index(self) -> None:
        self._bm25 = None
        self._idf = None

    # -- accessors --------------------------------------------------------- #
    def chunks(self) -> list[Chunk]:
        return list(self._chunks.values())

    def documents(self) -> list[Document]:
        return list(self._documents.values())

    def get_document(self, doc_id: str) -> Document | None:
        return self._documents.get(doc_id)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def summaries(self) -> list[DocumentSummary]:
        summaries = []
        for doc in self._documents.values():
            doc_chunks = [c for c in self._chunks.values() if c.doc_id == doc.doc_id]
            summaries.append(
                DocumentSummary(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    source=doc.source,
                    kind=doc.kind,
                    chunks=len(doc_chunks),
                    characters=len(doc.content),
                    created_at=doc.created_at,
                    metadata={
                        k: v
                        for k, v in doc.metadata.items()
                        if k in {"topic", "tags", "author", "date", "url", "type"}
                    },
                )
            )
        return sorted(summaries, key=lambda s: s.title)

    def topics(self) -> list[str]:
        topics: set[str] = set()
        for doc in self._documents.values():
            topic = doc.metadata.get("topic")
            if isinstance(topic, str) and topic:
                topics.add(topic)
            tags = doc.metadata.get("tags")
            if isinstance(tags, list):
                topics.update(str(t) for t in tags if t)
        return sorted(topics)

    def idf_map(self) -> dict[str, float]:
        """Inverse document frequency per token (cached until the corpus changes)."""
        if self._idf is not None:
            return self._idf
        df: Counter[str] = Counter()
        for chunk in self._chunks.values():
            df.update(set(tokenize(chunk.content)))
        total = max(len(self._chunks), 1)
        self._idf = {term: math.log(1 + total / (1 + count)) for term, count in df.items()}
        return self._idf

    def stats(self) -> dict[str, Any]:
        total_chars = sum(len(c.content) for c in self._chunks.values())
        return {
            "documents": len(self._documents),
            "chunks": len(self._chunks),
            "characters": total_chars,
            "avg_chunk_chars": round(total_chars / max(len(self._chunks), 1), 1),
            "dimension": self.dimension,
            "topics": self.topics(),
        }

    # -- search ------------------------------------------------------------ #
    def dense_search(
        self, vector: list[float], *, k: int = 10, filters: dict[str, Any] | None = None
    ) -> list[RetrievedChunk]:
        hits: list[RetrievedChunk] = []
        for chunk in self._chunks.values():
            if not _matches(chunk, filters):
                continue
            score = cosine_similarity(vector, chunk.embedding or [])
            hits.append(RetrievedChunk(chunk=chunk, score=round(score, 6), retriever="dense"))
        hits.sort(key=lambda h: h.score, reverse=True)
        return _with_rank(hits[:k])

    def keyword_search(
        self, query: str, *, k: int = 10, filters: dict[str, Any] | None = None
    ) -> list[RetrievedChunk]:
        index = self._bm25_index()
        raw = index.scores(tokenize(query))
        hits: list[RetrievedChunk] = []
        for idx, score in raw.items():
            chunk_id = self._bm25_ids[idx]
            chunk = self._chunks[chunk_id]
            if not _matches(chunk, filters):
                continue
            hits.append(
                RetrievedChunk(
                    chunk=chunk, score=round(score, 6), retriever="keyword", components={"bm25": score}
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return _with_rank(hits[:k])

    def hybrid_search(
        self,
        query: str,
        vector: list[float],
        *,
        k: int = 10,
        filters: dict[str, Any] | None = None,
        rrf_k: int = 60,
        dense_weight: float = 1.0,
        keyword_weight: float = 1.0,
    ) -> list[RetrievedChunk]:
        dense = self.dense_search(vector, k=k * 2, filters=filters)
        keyword = self.keyword_search(query, k=k * 2, filters=filters)
        fused: dict[str, RetrievedChunk] = {}
        fused_scores: dict[str, float] = {}
        components: dict[str, dict[str, float]] = {}
        for weight, hits, name in (
            (dense_weight, dense, "dense"),
            (keyword_weight, keyword, "keyword"),
        ):
            for rank, hit in enumerate(hits, start=1):
                cid = hit.chunk.chunk_id
                fused[cid] = hit
                fused_scores[cid] = fused_scores.get(cid, 0.0) + weight / (rrf_k + rank)
                components.setdefault(cid, {})[name] = round(hit.score, 6)
                components[cid][f"{name}_rank"] = float(rank)
        ordered = sorted(fused.values(), key=lambda h: fused_scores[h.chunk.chunk_id], reverse=True)[:k]
        result: list[RetrievedChunk] = []
        max_score = max((fused_scores[h.chunk.chunk_id] for h in ordered), default=1.0) or 1.0
        for hit in ordered:
            cid = hit.chunk.chunk_id
            result.append(
                RetrievedChunk(
                    chunk=hit.chunk,
                    score=round(fused_scores[cid] / max_score, 6),
                    retriever="hybrid",
                    components=components.get(cid, {}),
                )
            )
        return _with_rank(result)

    def _bm25_index(self) -> BM25Index:
        if self._bm25 is None:
            self._bm25_ids = list(self._chunks)
            self._bm25 = BM25Index([tokenize(self._chunks[cid].content) for cid in self._bm25_ids])
        return self._bm25

    # -- persistence ------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "documents": [d.model_dump() for d in self._documents.values()],
            "chunks": [c.model_dump() for c in self._chunks.values()],
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> VectorStore:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls(dimension=int(payload.get("dimension", 384)))
        for doc in payload.get("documents", []):
            store.upsert_document(Document.model_validate(doc))
        store.add_chunks([Chunk.model_validate(c) for c in payload.get("chunks", [])])
        return store


def _matches(chunk: Chunk, filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        actual = chunk.metadata.get(key, getattr(chunk, key, None))
        if isinstance(expected, list | tuple | set):
            if isinstance(actual, list):
                if not set(expected) & set(actual):
                    return False
            elif actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _with_rank(hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
    return [hit.model_copy(update={"rank": idx}) for idx, hit in enumerate(hits, start=1)]
