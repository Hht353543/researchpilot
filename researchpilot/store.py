"""Run store: keeps research results in memory and on disk (``runs/``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from researchpilot.config import get_settings
from researchpilot.schemas import ResearchResult


class ResultStore:
    def __init__(self, runs_dir: str | Path | None = None, *, persist: bool = True) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir is not None else get_settings().runs_dir()
        self.persist = persist
        self._results: dict[str, ResearchResult] = {}
        self._lock = threading.Lock()

    def save(self, result: ResearchResult) -> None:
        with self._lock:
            self._results[result.task_id] = result
        if self.persist:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            path = self.runs_dir / f"{result.task_id}.result.json"
            path.write_text(
                json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def get(self, task_id: str) -> ResearchResult | None:
        result = self._results.get(task_id)
        if result is not None:
            return result
        path = self.runs_dir / f"{task_id}.result.json"
        if not path.exists():
            return None
        try:
            result = ResearchResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # pragma: no cover - corrupted or foreign json file
            return None
        with self._lock:
            self._results[task_id] = result
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
