"""Embedding providers: deterministic offline hashing + OpenAI-compatible API."""

from __future__ import annotations

import hashlib
import itertools
import math
from abc import ABC, abstractmethod
from typing import Any

import httpx

from researchpilot.config import Settings, get_settings
from researchpilot.utils import normalize_text


class Embedder(ABC):
    name: str = "base"
    dimension: int = 0

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedder(Embedder):
    """Feature-hashing embedder (unigrams + CJK bigrams + char trigrams).

    Deterministic, dependency-free and good enough for hybrid retrieval
    baselines; swap in a real embedding model through config for production.
    """

    name = "hash"

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0:
            return vector
        return [round(v / norm, 6) for v in vector]

    @staticmethod
    def _features(text: str) -> list[tuple[str, float]]:
        lowered = normalize_text(text)
        tokens = [t for t in _word_tokens(lowered) if t]
        features: list[tuple[str, float]] = [(f"w:{t}", 1.0) for t in tokens]
        cjk = [ch for ch in lowered if _is_cjk(ch)]
        features.extend((f"c:{ch}", 0.6) for ch in cjk)
        features.extend((f"b:{a}{b}", 0.8) for a, b in itertools.pairwise(cjk))
        compact = lowered.replace(" ", "")
        features.extend((f"t:{compact[i : i + 3]}", 0.4) for i in range(max(len(compact) - 2, 0)))
        return features


class OpenAIEmbedder(Embedder):
    """Embeddings from any OpenAI-compatible ``/embeddings`` endpoint."""

    name = "openai"

    def __init__(self, settings: Settings | None = None, *, batch_size: int = 32) -> None:
        self.settings = settings or get_settings()
        if not self.settings.api_key:
            raise ValueError("API_KEY is required for the openai embedding provider")
        self.batch_size = batch_size
        self.dimension = self.settings.embedding_dim
        self._client = httpx.Client(
            base_url=self.settings.base_url.rstrip("/"),
            timeout=self.settings.request_timeout_s,
            headers={"Authorization": f"Bearer {self.settings.api_key}"},
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = self._client.post(
                "/embeddings",
                json={"model": self.settings.embedding_model, "input": batch},
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            ordered = sorted(payload["data"], key=lambda d: d.get("index", 0))
            vectors.extend([list(map(float, item["embedding"])) for item in ordered])
        if vectors:
            self.dimension = len(vectors[0])
        return vectors


class SentenceTransformerEmbedder(Embedder):  # pragma: no cover - optional heavy extra
    """Local transformer embeddings (requires the ``embeddings`` extra)."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name)
        self.dimension = int(self.model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def build_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings)
    return HashEmbedder(settings.embedding_dim)


def _word_tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9_]+", text)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF
