"""Long-term memory: durable, TTL-aware, de-duplicated, importance-ranked."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from researchpilot.config import get_settings
from researchpilot.memory.models import MemoryKind, MemoryRecord
from researchpilot.utils import jaccard, normalize_text, parse_iso, sha1_of, utc_now_iso


class LongTermMemory:
    """JSON-backed store with TTL, dedup and importance/recency recall ranking."""

    def __init__(self, path: str | Path | None = None, *, dedup_threshold: float = 0.82) -> None:
        base = Path(path) if path is not None else get_settings().runs_dir() / "long_term_memory.json"
        self.path = base
        self.dedup_threshold = dedup_threshold
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.Lock()
        self._load()

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - corrupted file
            return
        for item in payload.get("records", []):
            record = MemoryRecord.model_validate(item)
            self._records[record.id] = record
        self.forget_expired()

    def flush(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"records": [r.model_dump() for r in self._records.values()]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.path

    # -- write -------------------------------------------------------------- #
    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind = "fact",
        importance: float = 0.5,
        source: str = "",
        ttl_days: float | None = None,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        with self._lock:
            existing = self._find_duplicate(content)
            if existing is not None:
                existing.hits += 1
                if importance >= existing.importance:
                    existing.content = content
                    existing.importance = round(min(max(importance, existing.importance), 1.0), 3)
                    existing.created_at = utc_now_iso()
                self.flush()
                return existing
            record = MemoryRecord(
                id=f"mem_{sha1_of(normalize_text(content))}",
                content=content,
                kind=kind,
                importance=round(importance, 3),
                source=source,
                created_at=utc_now_iso(),
                expires_at=(
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_days * 86_400))
                    if ttl_days
                    else None
                ),
                metadata=dict(metadata or {}),
            )
            self._records[record.id] = record
            self.flush()
            return record

    def _find_duplicate(self, content: str) -> MemoryRecord | None:
        normalized = normalize_text(content)
        for record in self._records.values():
            other = normalize_text(record.content)
            if other == normalized or jaccard(other, normalized) >= self.dedup_threshold:
                return record
        return None

    # -- read --------------------------------------------------------------- #
    def recall(
        self, query: str = "", *, limit: int = 5, kind: MemoryKind | None = None
    ) -> list[MemoryRecord]:
        now = time.time()
        self.forget_expired(now=now)
        scored: list[tuple[float, MemoryRecord]] = []
        for record in self._records.values():
            if kind and record.kind != kind:
                continue
            similarity = jaccard(query, record.content) if query else 1.0
            recency = 1.0 / (1.0 + _age_days(record.created_at, now) / 30.0)
            hits = min(record.hits / 5.0, 1.0)
            score = 0.45 * record.importance + 0.25 * recency + 0.2 * similarity + 0.1 * hits
            if query and similarity == 0.0:
                score *= 0.5
            scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[MemoryRecord] = []
        for _, record in scored[:limit]:
            record.hits += 1
            results.append(record)
        if results:
            # Persist hit counts, otherwise the importance/recency ranking loses
            # its "frequently used" signal on the next process start.
            self.flush()
        return results

    def forget_expired(self, *, now: float | None = None) -> int:
        moment = now if now is not None else time.time()
        expired = [rid for rid, record in self._records.items() if record.is_expired(now=moment)]
        for rid in expired:
            del self._records[rid]
        if expired:
            self.flush()
        return len(expired)

    def all(self) -> list[MemoryRecord]:
        return list(self._records.values())

    def stats(self) -> dict[str, object]:
        return {
            "records": len(self._records),
            "by_kind": {
                kind: sum(1 for r in self._records.values() if r.kind == kind)
                for kind in {r.kind for r in self._records.values()}
            },
            "avg_importance": round(
                sum(r.importance for r in self._records.values()) / max(len(self._records), 1), 3
            ),
        }

    def clear(self) -> None:
        self._records.clear()
        self.flush()


def _age_days(created_at: str, now: float) -> float:
    stamp = parse_iso(created_at)
    if stamp is None:
        return 0.0
    return max((now - stamp) / 86_400, 0.0)
