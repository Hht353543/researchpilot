"""Run store: keeps research results in memory and on disk (``runs/``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from researchpilot.config import get_settings
from researchpilot.persistence import PersistenceCorruptionError, PersistenceError, atomic_write_text
from researchpilot.schemas import ResearchResult


class ResultStore:
    def __init__(self, runs_dir: str | Path | None = None, *, persist: bool = True) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir is not None else get_settings().runs_dir()
        self.persist = persist
        self._results: dict[str, ResearchResult] = {}
        self._lock = threading.Lock()

    def save(self, result: ResearchResult) -> None:
        self.save_durable(result)
        self.publish(result)

    def save_durable(self, result: ResearchResult) -> None:
        """Commit the artifact without making it visible in this process yet."""
        if self.persist:
            path = self.runs_dir / f"{result.task_id}.result.json"
            try:
                atomic_write_text(
                    path,
                    json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
                )
            except Exception as exc:
                raise PersistenceError("research result could not be committed") from exc

    def publish(self, result: ResearchResult) -> None:
        with self._lock:
            self._results[result.task_id] = result

    def publish_transient(self, result: ResearchResult) -> None:
        """Expose an explicitly degraded result without claiming it is durable."""
        self.publish(result)

    def get(self, task_id: str) -> ResearchResult | None:
        result = self._results.get(task_id)
        if result is not None:
            return result
        result = self.read_durable(task_id)
        if result is not None:
            with self._lock:
                self._results[task_id] = result
        return result

    def read_durable(self, task_id: str) -> ResearchResult | None:
        """Read the artifact from disk, bypassing the process-local publication cache."""
        path = self.runs_dir / f"{task_id}.result.json"
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PersistenceError("research result could not be read") from exc
        try:
            result = ResearchResult.model_validate(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            raise PersistenceCorruptionError("research result is corrupted") from exc
        return result

    def all(self, *, limit: int = 20) -> list[ResearchResult]:
        results: list[ResearchResult] = []
        if self.runs_dir.exists():
            files = sorted(
                self.runs_dir.glob("*.result.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in files[:limit]:
                result = self.get(path.name[: -len(".result.json")])
                if result is not None:
                    results.append(result)
        return results

    def task_ids(self) -> list[str]:
        ids = set(self._results)
        if self.runs_dir.exists():
            ids.update(p.name[: -len(".result.json")] for p in self.runs_dir.glob("*.result.json"))
        return sorted(ids)
