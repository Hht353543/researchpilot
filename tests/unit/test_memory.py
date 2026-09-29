"""Three-layer memory: bounds, TTL, dedup, importance ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from researchpilot.memory.long_term import LongTermMemory
from researchpilot.memory.manager import MemoryManager
from researchpilot.memory.short_term import ShortTermMemory
from researchpilot.memory.working import WorkingMemory
from researchpilot.persistence import PersistenceError
from researchpilot.schemas import Evidence, ResearchPlan, Subtask


def test_short_term_memory_respects_token_budget() -> None:
    memory = ShortTermMemory(max_tokens=40, max_items=50)
    for index in range(20):
        memory.add("agent", f"observation {index} " + "x" * 40)
    assert memory.total_tokens() <= 40
    assert len(memory.items) < 20
    assert memory.to_prompt(limit=2).count("\n") <= 1


def test_working_memory_dedupes_evidence_and_tracks_gaps() -> None:
    memory = WorkingMemory(task_id="t")
    memory.set_plan(
        ResearchPlan(
            objective="o",
            subtasks=[
                Subtask(
                    id="S1",
                    question="q1",
                    intent="knowledge_search",
                    tools=["knowledge_search"],
                    expected_output="x",
                ),
                Subtask(
                    id="S2",
                    question="q2",
                    intent="web_search",
                    tools=["web_search"],
                    expected_output="y",
                ),
            ],
        )
    )
    item = Evidence(id="E1", subtask_id="S1", claim="claim", quote="quote", source_id="s1")
    added = memory.add_evidence([item, item.model_copy(update={"id": "E2"})])
    assert len(added) == 1
    assert len(memory.evidence) == 1
    assert [s.id for s in memory.uncovered_subtasks()] == ["S2"]
    assert memory.token_estimate() > 0


def test_working_memory_keeps_distinct_claims() -> None:
    memory = WorkingMemory(task_id="t")
    memory.add_evidence([Evidence(id="E1", claim="MCP 基于 JSON-RPC", quote="q1", source_id="s1")])
    added = memory.add_evidence(
        [Evidence(id="E2", claim="工具注册表声明超时策略", quote="q2", source_id="s2")]
    )
    assert len(added) == 1
    assert len(memory.evidence) == 2


def test_long_term_memory_dedup_ttl_and_recall(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path / "ltm.json")
    first = memory.remember("用户偏好中文报告", kind="preference", importance=0.8, source="test")
    duplicate = memory.remember("用户偏好中文报告", kind="preference", importance=0.9, source="test")
    assert first.id == duplicate.id
    assert duplicate.hits >= 1
    memory.remember("短期事实将被过期", kind="fact", importance=0.5, ttl_days=-1)
    assert memory.forget_expired() == 1
    recalled = memory.recall("中文报告", limit=3)
    assert recalled and recalled[0].content.startswith("用户偏好")
    assert memory.stats()["records"] == 1


def test_long_term_memory_add_keeps_existing_records(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path / "ltm.json")
    first = memory.remember("record A", importance=0.4, source="first")
    second = memory.remember("record B", importance=0.6, source="second")

    assert {record.id for record in memory.all()} == {first.id, second.id}


def test_long_term_memory_duplicate_update_preserves_other_records(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path / "ltm.json")
    first = memory.remember("record A", importance=0.4, source="first")
    second = memory.remember("record B", importance=0.6, source="second")
    second_before = second.model_copy(deep=True)

    updated = memory.remember("record A", importance=0.9, source="updated")

    assert updated.id == first.id
    assert updated.importance == 0.9
    assert next(record for record in memory.all() if record.id == second.id) == second_before


def test_long_term_memory_reload_keeps_all_records(tmp_path: Path) -> None:
    path = tmp_path / "ltm.json"
    memory = LongTermMemory(path)
    memory.remember("record A")
    memory.remember("record B")

    reloaded = LongTermMemory(path)

    assert {record.content for record in reloaded.all()} == {"record A", "record B"}


def test_long_term_memory_remembering_unknown_content_inserts_it(tmp_path: Path) -> None:
    memory = LongTermMemory(tmp_path / "ltm.json")
    memory.remember("record A")

    created = memory.remember("previously unknown record")

    assert created in memory.all()
    assert len(memory.all()) == 2


def test_long_term_memory_remember_failure_keeps_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ltm.json"
    memory = LongTermMemory(path)
    memory.remember("committed record")
    before_disk = path.read_bytes()
    before_memory = [record.model_dump() for record in memory.all()]

    monkeypatch.setattr(
        "researchpilot.memory.long_term.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
    )
    with pytest.raises(PersistenceError):
        memory.remember("uncommitted record")

    assert [record.model_dump() for record in memory.all()] == before_memory
    assert path.read_bytes() == before_disk


def test_long_term_memory_clear_failure_keeps_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ltm.json"
    memory = LongTermMemory(path)
    memory.remember("committed record")
    before_disk = path.read_bytes()

    monkeypatch.setattr(
        "researchpilot.memory.long_term.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
    )
    with pytest.raises(PersistenceError):
        memory.clear()

    assert [record.content for record in memory.all()] == ["committed record"]
    assert path.read_bytes() == before_disk


def test_long_term_memory_recall_failure_does_not_publish_hit_increment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ltm.json"
    memory = LongTermMemory(path)
    memory.remember("committed record")
    before_disk = path.read_bytes()

    monkeypatch.setattr(
        "researchpilot.memory.long_term.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
    )
    with pytest.raises(PersistenceError):
        memory.recall("committed")

    assert memory.all()[0].hits == 0
    assert path.read_bytes() == before_disk


def test_long_term_memory_cleanup_failure_keeps_expired_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ltm.json"
    memory = LongTermMemory(path)
    memory.remember("expired but committed", ttl_days=-1)
    before_disk = path.read_bytes()

    monkeypatch.setattr(
        "researchpilot.memory.long_term.atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated write failure")),
    )
    with pytest.raises(PersistenceError):
        memory.forget_expired()

    assert [record.content for record in memory.all()] == ["expired but committed"]
    assert path.read_bytes() == before_disk


def test_memory_manager_persists_task_results(tmp_path: Path) -> None:
    manager = MemoryManager(task_id="t", long_term=LongTermMemory(tmp_path / "ltm.json"))
    stored = manager.persist_task_result(
        objective="研究 MCP 生态",
        conclusions=[("MCP 基于 JSON-RPC 2.0", 0.8), ("低置信结论", 0.2)],
    )
    assert stored == 2  # topic + one conclusion
    assert "MCP" in manager.recall_context("MCP", limit=3)
    assert manager.snapshot()["long_term_records"] == 2
