"""Retriever: query rewrite -> hybrid search -> rerank -> context assembly."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.llm.prompts import (
    QUERY_REWRITE_SYSTEM,
    RERANK_SYSTEM,
    query_rewrite_user,
    rerank_user,
)
from researchpilot.llm.structured import StructuredLLMRunner
from researchpilot.observability.trace import Tracer
from researchpilot.rag.embeddings import Embedder
from researchpilot.rag.models import RetrievedChunk
from researchpilot.rag.vector_store import VectorStore
from researchpilot.schemas import RerankScores
from researchpilot.utils import content_tokens, token_set, truncate

Strategy = Literal["dense", "keyword", "hybrid"]


class RetrievalResult(BaseModel):
    query: str
    rewritten_query: str = ""
    strategy: Strategy = "hybrid"
    filters: dict[str, Any] = Field(default_factory=dict)
    hits: list[RetrievedChunk] = Field(default_factory=list)
    context: str = ""
    rerank_strategy: str = "heuristic"
    candidates: int = 0
    latency_ms: float = 0.0

    def items(self) -> list[dict[str, Any]]:
        return [hit.as_item() for hit in self.hits]

    def source_ids(self) -> list[str]:
        return [hit.chunk.chunk_id for hit in self.hits]


class Retriever:
    """Retrieval as a measurable module (see ``tests/evaluation``)."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        settings: Settings | None = None,
        llm_runner: StructuredLLMRunner | None = None,
        tracer: Tracer | None = None,
        rewrite_enabled: bool = True,
        rerank_strategy: Literal["heuristic", "llm"] = "heuristic",
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings or get_settings()
        self.llm_runner = llm_runner
        self.tracer = tracer
        self.rewrite_enabled = rewrite_enabled
        self.rerank_strategy = rerank_strategy

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        retrieve_k: int | None = None,
        strategy: Strategy = "hybrid",
        filters: dict[str, Any] | None = None,
        rewrite: bool | None = None,
        rewrite_with_llm: bool | None = None,
        rerank: bool = True,
        max_context_chars: int | None = None,
        agent: str = "Retriever",
    ) -> RetrievalResult:
        top_k = top_k or self.settings.top_k
        retrieve_k = retrieve_k or max(self.settings.retrieve_k, top_k)
        rewrite = self.rewrite_enabled if rewrite is None else rewrite
        rewrite_with_llm = (self.llm_runner is not None) if rewrite_with_llm is None else rewrite_with_llm
        max_context_chars = max_context_chars or self.settings.max_context_chars

        tracer = self.tracer
        span = (
            tracer.start_span(
                f"{agent}.retrieval[{strategy}]",
                "retrieval",
                agent=agent,
                input={"query": query, "filters": filters or {}, "top_k": top_k},
            )
            if tracer
            else None
        )
        started = time.perf_counter()
        try:
            rewritten = self._rewrite(query, use_llm=rewrite, with_llm=bool(rewrite_with_llm))
            vector = self.embedder.embed_one(rewritten)
            if strategy == "dense":
                hits = self.store.dense_search(vector, k=retrieve_k, filters=filters)
            elif strategy == "keyword":
                hits = self.store.keyword_search(rewritten, k=retrieve_k, filters=filters)
            else:
                hits = self.store.hybrid_search(rewritten, vector, k=retrieve_k, filters=filters)
            candidates = len(hits)
            hits = self.rerank(query, hits, k=top_k) if rerank and hits else hits[:top_k]
            context = self.format_context(hits, max_chars=max_context_chars)
            result = RetrievalResult(
                query=query,
                rewritten_query=rewritten,
                strategy=strategy,
                filters=filters or {},
                hits=hits,
                context=context,
                rerank_strategy=self.rerank_strategy if rerank else "none",
                candidates=candidates,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            if span is not None:
                tracer.end_span(  # type: ignore[union-attr]
                    span,
                    output={
                        "rewritten_query": rewritten,
                        "doc_ids": [h.chunk.doc_id for h in hits],
                        "hits": [
                            {"chunk_id": h.chunk.chunk_id, "score": h.score, "rank": h.rank} for h in hits
                        ],
                        "context_chars": len(context),
                    },
                    metadata={"candidates": candidates, "rerank": result.rerank_strategy},
                )
            return result
        except Exception as exc:
            if span is not None:
                tracer.end_span(span, error=f"{type(exc).__name__}: {exc}")  # type: ignore[union-attr]
            raise

    # -- stages ------------------------------------------------------------ #
    def _rewrite(self, query: str, *, use_llm: bool, with_llm: bool) -> str:
        if not use_llm:
            return query
        if with_llm and self.llm_runner is not None:
            try:
                result = self.llm_runner.run_text(
                    agent="Retriever",
                    system=QUERY_REWRITE_SYSTEM,
                    user=query_rewrite_user(query, self.store.topics()),
                    purpose="query_rewrite",
                    hints={"query": query, "known_topics": self.store.topics()},
                    max_tokens=120,
                )
                candidate = result.text.strip().splitlines()[0] if result.text.strip() else ""
                if 2 < len(candidate) <= 300:
                    return candidate
            except Exception:
                return query
        return query

    def rerank(self, query: str, hits: list[RetrievedChunk], *, k: int) -> list[RetrievedChunk]:
        if self.rerank_strategy == "llm" and self.llm_runner is not None:
            reranked = self._rerank_with_llm(query, hits, k)
            if reranked:
                return reranked
        return self._rerank_heuristic(query, hits, k)

    def _rerank_heuristic(self, query: str, hits: list[RetrievedChunk], k: int) -> list[RetrievedChunk]:
        q_tokens = content_tokens(query) or token_set(query)
        idf = self.store.idf_map()
        default_idf = max(idf.values(), default=1.0)
        total_weight = sum(idf.get(token, default_idf) for token in q_tokens) or 1.0
        max_retrieval = max((h.score for h in hits), default=1.0) or 1.0
        rescored: list[RetrievedChunk] = []
        for hit in hits:
            section = str(hit.chunk.metadata.get("section", ""))
            body_tokens = content_tokens(hit.chunk.content)
            header_tokens = content_tokens(f"{hit.chunk.title} {section}")
            body_cover = sum(idf.get(t, default_idf) for t in q_tokens if t in body_tokens) / total_weight
            header_cover = sum(idf.get(t, default_idf) for t in q_tokens if t in header_tokens) / total_weight
            lexical = len(q_tokens & body_tokens) / max(len(q_tokens), 1)
            retrieval_norm = hit.score / max_retrieval
            components = {
                "body_coverage": round(body_cover, 4),
                "header_coverage": round(header_cover, 4),
                "lexical": round(lexical, 4),
                "retrieval": round(retrieval_norm, 4),
            }
            score = 0.45 * body_cover + 0.25 * header_cover + 0.15 * lexical + 0.15 * retrieval_norm
            rescored.append(
                hit.model_copy(
                    update={"score": round(score, 6), "retriever": "rerank", "components": components}
                )
            )
        rescored.sort(key=lambda h: h.score, reverse=True)
        return [h.model_copy(update={"rank": i}) for i, h in enumerate(rescored[:k], start=1)]

    def _rerank_with_llm(self, query: str, hits: list[RetrievedChunk], k: int) -> list[RetrievedChunk]:
        assert self.llm_runner is not None
        passages = [{"id": h.chunk.chunk_id, "content": truncate(h.chunk.content, 500)} for h in hits]
        try:
            result = self.llm_runner.run(
                RerankScores,
                agent="Retriever",
                system=RERANK_SYSTEM,
                user=rerank_user(query, passages),
                hints={"query": query, "passages": passages},
                purpose="rerank",
                max_tokens=600,
            )
        except Exception:
            return []
        scores = {s.id: s.score for s in result.value.scores}
        if not scores:
            return []
        reordered = sorted(hits, key=lambda h: scores.get(h.chunk.chunk_id, 0.0), reverse=True)[:k]
        return [
            h.model_copy(
                update={
                    "score": round(scores.get(h.chunk.chunk_id, 0.0), 6),
                    "retriever": "rerank-llm",
                }
            )
            for h in reordered
        ]

    @staticmethod
    def format_context(hits: list[RetrievedChunk], *, max_chars: int = 12_000) -> str:
        lines: list[str] = []
        used = 0
        for hit in hits:
            header = f"[{hit.chunk.chunk_id}] {hit.chunk.title} ({hit.chunk.source})"
            body = truncate(hit.chunk.content, 1200)
            block = f"{header}\n{body}"
            if used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block)
        return "\n\n".join(lines)
