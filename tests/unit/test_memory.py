"""Three-layer memory: bounds, TTL, dedup, importance ranking."""

from __future__ import annotations

from pathlib import Path

from researchpilot.memory.long_term import LongTermMemory
from researchpilot.memory.manager import MemoryManager
from researchpilot.memory.short_term import ShortTermMemory
from researchpilot.memory.working import WorkingMemory
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


def test_memory_manager_persists_task_results(tmp_path: Path) -> None:
    manager = MemoryManager(task_id="t", long_term=LongTermMemory(tmp_path / "ltm.json"))
    stored = manager.persist_task_result(
        objective="研究 MCP 生态",
        conclusions=[("MCP 基于 JSON-RPC 2.0", 0.8), ("低置信结论", 0.2)],
    )
    assert stored == 2  # topic + one conclusion
    assert "MCP" in manager.recall_context("MCP", limit=3)
    assert manager.snapshot()["long_term_records"] == 2
