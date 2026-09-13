"""Web search backends: bundled offline corpus (default) or a real HTTP search API."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from researchpilot.config import Settings, get_settings
from researchpilot.rag.embeddings import HashEmbedder
from researchpilot.rag.vector_store import VectorStore
from researchpilot.utils import resolve_input_path, truncate


class OfflineWebBackend:
    """Deterministic search over the bundled synthetic web corpus.

    The corpus lives in ``data/web_corpus`` and is clearly marked as synthetic
    sample data: it makes the whole system runnable (and benchmarkable) offline
    without pretending to be live search results.
    """

    name = "offline-corpus"

    def __init__(self, corpus_dir: str | Path, *, dimension: int = 256) -> None:
        self.corpus_dir = resolve_input_path(corpus_dir)
        self.embedder = HashEmbedder(dimension)
        self._store: VectorStore | None = None
        self.corpus_missing = not self.corpus_dir.exists()

    def _ensure_store(self) -> VectorStore:
        if self._store is not None:
            return self._store
        store = VectorStore(dimension=self.embedder.dimension)
        records: list[dict[str, Any]] = []
        if self.corpus_dir.exists():
            for file in sorted(self.corpus_dir.glob("*.jsonl")):
                for line in file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        if records:
            from researchpilot.rag.models import Chunk, Document

            documents = []
            chunks = []
            for idx, record in enumerate(records):
                url = str(record.get("url") or f"offline://web/{idx}")
                doc_id = f"web_{idx:03d}"
                body = str(record.get("content") or record.get("snippet") or "")
                documents.append(
                    Document(
                        doc_id=doc_id,
                        title=str(record.get("title") or url),
                        source=url,
                        kind="web",
                        content=body,
                        metadata={
                            "synthetic": True,
                            "date": record.get("date", ""),
                            "publisher": record.get("publisher", ""),
                            "url": url,
                        },
                    )
                )
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}#c000",
                        doc_id=doc_id,
                        title=str(record.get("title") or url),
                        source=url,
                        kind="web",
                        content=body,
                        metadata={
                            "synthetic": True,
                            "date": record.get("date", ""),
                            "publisher": record.get("publisher", ""),
                            "url": url,
                        },
                    )
                )
            for document in documents:
                store.upsert_document(document)
            vectors = self.embedder.embed([c.content for c in chunks])
            store.add_chunks([c.with_embedding(v) for c, v in zip(chunks, vectors, strict=True)])
        self._store = store
        return store

    def search(self, query: str, *, top_k: int = 5, recency_days: int | None = None) -> list[dict[str, Any]]:
        store = self._ensure_store()
        vector = self.embedder.embed_one(query)
        hits = store.hybrid_search(query, vector, k=max(top_k * 2, top_k))
        results: list[dict[str, Any]] = []
        now = time.time()
        for hit in hits:
            metadata = hit.chunk.metadata
            date = str(metadata.get("date") or "")
            if recency_days and date:
                parsed = _parse_date(date)
                if parsed is not None and (now - parsed) > recency_days * 86_400:
                    continue
            results.append(
                {
                    "source_id": f"web:{hit.chunk.doc_id}",
                    "doc_id": hit.chunk.doc_id,
                    "title": hit.chunk.title,
                    "url": metadata.get("url", hit.chunk.source),
                    "date": date,
                    "publisher": metadata.get("publisher", ""),
                    "snippet": truncate(hit.chunk.content, 240),
                    "content": hit.chunk.content,
                    "score": hit.score,
                    "synthetic": bool(metadata.get("synthetic", True)),
                }
            )
            if len(results) >= top_k:
                break
        return results


class HttpWebBackend:
    """Calls a real search API: ``GET {url}?q=...&limit=...`` -> ``{"results": [...]}``."""

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 10.0,
        api_key: str | None = None,
        allowed_hosts: str | list[str] | None = None,
    ) -> None:
        if not url:
            raise ValueError("RESEARCHPILOT_WEB_SEARCH_URL is required for the http backend")
        # SSRF hardening: the URL is operator-configured (never model-controlled),
        # but we still refuse non-HTTP schemes, URL-embedded credentials and hosts
        # outside an optional allow-list.
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"web search URL must be http(s), got scheme {parsed.scheme!r}")
        if parsed.username or parsed.password:
            raise ValueError("web search URL must not embed credentials")
        if not parsed.hostname:
            raise ValueError("web search URL must include a hostname")
        self.url = url
        self.timeout_s = timeout_s
        self.api_key = api_key
        self.allowed_hosts = _parse_hosts(allowed_hosts)
        if self.allowed_hosts and parsed.hostname not in self.allowed_hosts:
            raise ValueError(
                f"web search host {parsed.hostname!r} is not in the allow-list {self.allowed_hosts}"
            )

    def search(self, query: str, *, top_k: int = 5, recency_days: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query, "limit": top_k}
        if recency_days:
            params["days"] = recency_days
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.get(self.url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        results = payload.get("results", payload if isinstance(payload, list) else [])
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(results[:top_k]):
            url = str(item.get("url") or item.get("link") or "")
            normalized.append(
                {
                    "source_id": f"web:{url or idx}",
                    "doc_id": f"web_http_{idx:03d}",
                    "title": str(item.get("title") or url),
                    "url": url,
                    "date": str(item.get("date") or item.get("published") or ""),
                    "publisher": str(item.get("publisher") or item.get("source") or ""),
                    "snippet": str(item.get("snippet") or truncate(str(item.get("content", "")), 240)),
                    "content": str(item.get("content") or item.get("snippet") or ""),
                    "score": float(item.get("score") or 0.0),
                    "synthetic": False,
                }
            )
        return normalized


def build_web_backend(settings: Settings | None = None) -> OfflineWebBackend | HttpWebBackend:
    settings = settings or get_settings()
    if settings.web_search_mode == "http":
        return HttpWebBackend(
            settings.web_search_url,
            timeout_s=settings.web_search_timeout_s,
            allowed_hosts=settings.web_search_allowed_hosts,
        )
    return OfflineWebBackend(settings.web_corpus_path)


def _parse_hosts(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [host.strip() for host in value.split(",") if host.strip()]
    return [str(host).strip() for host in value if str(host).strip()]


def _parse_date(value: str) -> float | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m"):
        try:
            return time.mktime(time.strptime(value[: len(fmt) + 2], fmt))
        except ValueError:
            continue
    return None
