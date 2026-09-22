"""Long-term memory: durable, TTL-aware, de-duplicated, importance-ranked."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from researchpilot.config import get_settings
from researchpilot.memory.models import MemoryKind, MemoryRecord
from researchpilot.persistence import (
    PersistenceCorruptionError,
    PersistenceError,
    StorageSignature,
    atomic_write_text,
    serialized_file_update,
    storage_signature,
)
from researchpilot.utils import jaccard, normalize_text, parse_iso, sha1_of, utc_now_iso


class LongTermMemory:
    """JSON-backed store with TTL, dedup and importance/recency recall ranking."""

    def __init__(self, path: str | Path | None = None, *, dedup_threshold: float = 0.82) -> None:
        base = Path(path) if path is not None else get_settings().runs_dir() / "long_term_memory.json"
        self.path = base
        self.dedup_threshold = dedup_threshold
        self._records: dict[str, MemoryRecord] = {}
        self._lock = threading.RLock()
        self._storage_signature: StorageSignature | None = None
        self._load()

    # -- persistence -------------------------------------------------------- #
    def _read_records(self) -> dict[str, MemoryRecord]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PersistenceError("long-term memory could not be read") from exc
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise TypeError("records must be an array")
            records = [MemoryRecord.model_validate(item) for item in payload["records"]]
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
            raise PersistenceCorruptionError("long-term memory is corrupted") from exc
        if len({record.id for record in records}) != len(records):
            raise PersistenceCorruptionError("long-term memory is corrupted")
        return {record.id: record for record in records}

    def _load(self) -> None:
        records = self._read_records()
        self._records = records
        self._storage_signature = storage_signature(self.path)

    def _refresh_if_changed(self) -> None:
        if storage_signature(self.path) != self._storage_signature:
            self._load()

    def _commit_records_unlocked(self, records: dict[str, MemoryRecord]) -> Path:
        payload = {"records": [record.model_dump() for record in records.values()]}
        try:
            saved = atomic_write_text(
                self.path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            raise PersistenceError("long-term memory update could not be committed") from exc
        self._records = records
        self._storage_signature = storage_signature(self.path)
        return saved

    def flush(self) -> Path:
        """Synchronize and rewrite the latest durable snapshot atomically."""
        with self._lock, serialized_file_update(self.path):
            records = self._read_records()
            return self._commit_records_unlocked(records)

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
        with self._lock, serialized_file_update(self.path):
            # Every mutation starts from the last committed snapshot. This is
            # what prevents a stale process instance from overwriting peers.
            records = self._read_records()
            existing = self._find_duplicate(records, content)
            if existing is not None:
                existing.hits += 1
                if importance >= existing.importance:
                    existing.content = content
                    existing.importance = round(min(max(importance, existing.importance), 1.0), 3)
                    existing.created_at = utc_now_iso()
                self._commit_records_unlocked(records)
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
            records[record.id] = record
            self._commit_records_unlocked(records)
            return record

    def _find_duplicate(self, records: dict[str, MemoryRecord], content: str) -> MemoryRecord | None:
        normalized = normalize_text(content)
        for record in records.values():
            other = normalize_text(record.content)
            if other == normalized or jaccard(other, normalized) >= self.dedup_threshold:
                return record
        return None

    # -- read --------------------------------------------------------------- #
    def recall(
        self, query: str = "", *, limit: int = 5, kind: MemoryKind | None = None
    ) -> list[MemoryRecord]:
        with self._lock, serialized_file_update(self.path):
            records = self._read_records()
            now = time.time()
            changed = self._forget_expired_unlocked(records, now)
            scored: list[tuple[float, MemoryRecord]] = []
            for record in records.values():
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
            if results or changed:
                self._commit_records_unlocked(records)
            else:
                self._records = records
                self._storage_signature = storage_signature(self.path)
            return results

    def forget_expired(self, *, now: float | None = None) -> int:
        with self._lock, serialized_file_update(self.path):
            records = self._read_records()
            expired = self._forget_expired_unlocked(records, now if now is not None else time.time())
            if expired:
                self._commit_records_unlocked(records)
            else:
                self._records = records
                self._storage_signature = storage_signature(self.path)
            return expired

    def _forget_expired_unlocked(self, records: dict[str, MemoryRecord], moment: float) -> int:
        expired = [rid for rid, record in records.items() if record.is_expired(now=moment)]
        for rid in expired:
            del records[rid]
        return len(expired)

    def all(self) -> list[MemoryRecord]:
        with self._lock:
            self._refresh_if_changed()
            return list(self._records.values())

    def stats(self) -> dict[str, object]:
        with self._lock:
            self._refresh_if_changed()
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
        with self._lock, serialized_file_update(self.path):
            self._read_records()  # corruption must stop a destructive overwrite
            self._commit_records_unlocked({})


def _age_days(created_at: str, now: float) -> float:
    stamp = parse_iso(created_at)
    if stamp is None:
        return 0.0
    return max((now - stamp) / 86_400, 0.0)
