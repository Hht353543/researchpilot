"""Run and trace stores publish only durable, schema-valid artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import researchpilot.persistence as persistence
import researchpilot.store as result_store_module
from researchpilot.observability import trace as trace_module
from researchpilot.observability.trace import Tracer, TraceStore
from researchpilot.persistence import PersistenceError
from researchpilot.schemas import ResearchResult
from researchpilot.store import ResultStore


def _result() -> ResearchResult:
    return ResearchResult(task_id="task-failure", trace_id="trace-failure", question="test")


def test_result_store_write_failure_does_not_publish_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ResultStore(tmp_path)
    monkeypatch.setattr(
        result_store_module,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
        raising=False,
    )

    with pytest.raises(PersistenceError):
        store.save(_result())

    assert store.get("task-failure") is None


def test_trace_store_write_failure_does_not_publish_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TraceStore(tmp_path)
    trace = Tracer("task-failure", "test").finish()
    monkeypatch.setattr(
        trace_module,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
        raising=False,
    )

    with pytest.raises(PersistenceError):
        store.save(trace)

    assert store.get("task-failure") is None


@pytest.mark.parametrize(
    ("suffix", "loader"),
    [
        ("result.json", lambda path: ResultStore(path.parent).get("broken")),
        ("trace.json", lambda path: TraceStore(path.parent).get("broken")),
    ],
)
def test_artifact_stores_reject_corrupted_json(
    tmp_path: Path, suffix: str, loader: Callable[[Path], object]
) -> None:
    path = tmp_path / f"broken.{suffix}"
    corrupted = '{"task_id":'
    path.write_text(corrupted, encoding="utf-8")
    corruption_error = getattr(persistence, "PersistenceCorruptionError", RuntimeError)

    with pytest.raises(corruption_error):
        loader(path)

    assert path.read_text(encoding="utf-8") == corrupted
