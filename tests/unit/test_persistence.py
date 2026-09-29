"""Persistence primitives must preserve the last committed JSON file."""

from __future__ import annotations

from pathlib import Path

import pytest

import researchpilot.persistence as persistence
from researchpilot.config import Settings
from researchpilot.memory.long_term import LongTermMemory
from researchpilot.persistence import PersistenceError, atomic_write_text, serialized_file_update
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.rag.vector_store import VectorStore


def test_atomic_write_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"state":"committed"}', encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("researchpilot.persistence.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_text(target, '{"state":"partial"}')

    assert target.read_text(encoding="utf-8") == '{"state":"committed"}'
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("failure", ["tempfile", "fsync"])
def test_atomic_write_pre_replace_failures_preserve_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"state":"committed"}', encoding="utf-8")
    if failure == "tempfile":
        monkeypatch.setattr(
            "researchpilot.persistence.tempfile.NamedTemporaryFile",
            lambda **_kwargs: (_ for _ in ()).throw(PermissionError("simulated tempfile failure")),
        )
    else:
        monkeypatch.setattr(
            "researchpilot.persistence.os.fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("simulated fsync failure")),
        )

    with pytest.raises(OSError):
        atomic_write_text(target, '{"state":"partial"}')

    assert target.read_text(encoding="utf-8") == '{"state":"committed"}'
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "content",
    [
        '{"records":',
        "not-json",
        '{"records":"not-a-list"}',
    ],
    ids=["truncated", "malformed", "invalid-schema"],
)
def test_long_term_memory_rejects_corrupted_json(tmp_path: Path, content: str) -> None:
    path = tmp_path / "memory.json"
    path.write_text(content, encoding="utf-8")
    corruption_error = getattr(persistence, "PersistenceCorruptionError", RuntimeError)

    with pytest.raises(corruption_error):
        LongTermMemory(path)

    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "content",
    [
        '{"documents":',
        "not-json",
        '{"dimension":64,"fingerprint":"hash:test:64","generation":1,"documents":"not-a-list","chunks":[]}',
    ],
    ids=["truncated", "malformed", "invalid-schema"],
)
def test_vector_store_rejects_corrupted_json(tmp_path: Path, content: str) -> None:
    path = tmp_path / "knowledge_base.json"
    path.write_text(content, encoding="utf-8")
    corruption_error = getattr(persistence, "PersistenceCorruptionError", RuntimeError)

    with pytest.raises(corruption_error):
        VectorStore.load(path)

    assert path.read_text(encoding="utf-8") == content


def test_knowledge_base_does_not_overwrite_corrupted_index(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "knowledge_base.json"
    path.parent.mkdir(parents=True)
    corrupted = '{"documents":'
    path.write_text(corrupted, encoding="utf-8")
    settings = Settings(
        _env_file=None,
        provider="mock",
        runs_path=str(path.parent),
        kb_path=str(tmp_path / "corpus"),
        embedding_dim=64,
    )
    corruption_error = getattr(persistence, "PersistenceCorruptionError", RuntimeError)

    with pytest.raises(corruption_error):
        KnowledgeBase.load_or_create(settings)

    assert path.read_text(encoding="utf-8") == corrupted


def test_lock_setup_failure_uses_persistence_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "researchpilot.persistence.sqlite3.connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private lock path")),
    )

    with (
        pytest.raises(PersistenceError, match="lock is unavailable"),
        serialized_file_update(tmp_path / "state.json"),
    ):
        pass
