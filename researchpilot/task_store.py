"""Durable task metadata and startup-recovery observations."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from researchpilot.config import get_settings
from researchpilot.persistence import (
    PersistenceCorruptionError,
    PersistenceError,
    atomic_write_text,
    serialized_file_update,
)
from researchpilot.schemas import ResearchResult, TaskRecord, TaskStatus, Trace

NON_TERMINAL_STATES = frozenset({"pending", "running"})


def task_now_iso() -> str:
    """Millisecond timestamp so short heartbeat leases remain observable."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TaskStore:
    """Cross-process-safe JSON task records under ``runs/``."""

    def __init__(self, runs_dir: str | Path | None = None) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir is not None else get_settings().runs_dir()

    def path_for(self, task_id: str) -> Path:
        return self.runs_dir / f"{task_id}.task.json"

    def create(self, record: TaskRecord) -> None:
        path = self.path_for(record.task_id)
        try:
            with serialized_file_update(path):
                if path.exists():
                    raise PersistenceError("task record already exists")
                self._write(path, record)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("task record could not be created") from exc

    def get(self, task_id: str) -> TaskRecord | None:
        path = self.path_for(task_id)
        if not path.exists():
            return None
        return self._read(path)

    def update(
        self,
        task_id: str,
        *,
        expected: set[TaskStatus] | frozenset[str] | None = None,
        owner_instance_id: str | None = None,
        mutate: Callable[[TaskRecord], TaskRecord],
    ) -> tuple[TaskRecord, bool]:
        """Atomically update one record, optionally as a status compare-and-swap."""
        path = self.path_for(task_id)
        try:
            with serialized_file_update(path):
                record = self._read(path)
                if expected is not None and record.status not in expected:
                    return record, False
                if owner_instance_id is not None and record.owner_instance_id != owner_instance_id:
                    return record, False
                updated = mutate(record)
                updated.updated_at = task_now_iso()
                self._write(path, updated)
                return updated, True
        except (PersistenceError, PersistenceCorruptionError):
            raise
        except Exception as exc:
            raise PersistenceError("task record could not be updated") from exc

    def all(self) -> list[TaskRecord]:
        if not self.runs_dir.exists():
            return []
        records: list[TaskRecord] = []
        for path in sorted(self.runs_dir.glob("*.task.json")):
            try:
                records.append(self._read(path))
            except PersistenceError:
                continue
        return records

    def observe_orphans(self) -> list[dict[str, str]]:
        """Record suspicious leftovers without deleting or modifying them."""
        if not self.runs_dir.exists():
            return []
        observations: list[dict[str, str]] = []
        for path in sorted(self.runs_dir.glob(".*.tmp")):
            observations.append(
                {"kind": "incomplete_temporary_file", "path": path.name, "observed_at": task_now_iso()}
            )
        for path in sorted(self.runs_dir.glob("*.task.json")):
            try:
                self._read(path)
            except PersistenceError:
                observations.append(
                    {"kind": "corrupted_task_record", "path": path.name, "observed_at": task_now_iso()}
                )
        for pattern, model, kind in (
            ("*.result.json", ResearchResult, "corrupted_result_artifact"),
            ("*.trace.json", Trace, "corrupted_trace_artifact"),
        ):
            for path in sorted(self.runs_dir.glob(pattern)):
                try:
                    model.model_validate(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
                    observations.append({"kind": kind, "path": path.name, "observed_at": task_now_iso()})
        if not observations:
            return []
        report_path = self.runs_dir / ".recovery-observations.json"
        try:
            with serialized_file_update(report_path):
                existing: list[dict[str, str]] = []
                if report_path.exists():
                    loaded = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        existing = [item for item in loaded if isinstance(item, dict)]
                known = {(item.get("kind"), item.get("path")) for item in existing}
                merged = existing + [
                    item for item in observations if (item["kind"], item["path"]) not in known
                ]
                atomic_write_text(report_path, json.dumps(merged, ensure_ascii=False, indent=2))
        except Exception as exc:
            raise PersistenceError("recovery observations could not be recorded") from exc
        return observations

    @staticmethod
    def _read(path: Path) -> TaskRecord:
        try:
            raw = path.read_text(encoding="utf-8")
            return TaskRecord.model_validate(json.loads(raw))
        except OSError as exc:
            raise PersistenceError("task record could not be read") from exc
        except (json.JSONDecodeError, UnicodeError, ValueError, TypeError) as exc:
            raise PersistenceCorruptionError("task record is corrupted") from exc

    @staticmethod
    def _write(path: Path, record: TaskRecord) -> None:
        atomic_write_text(
            path,
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )


def process_is_alive(pid: int | None) -> bool:
    """Best-effort same-host owner check used only with a freshness lease."""
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            exit_code = ctypes.c_ulong()
            try:
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and (
                    exit_code.value == 259
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
