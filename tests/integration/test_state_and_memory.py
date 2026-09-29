"""Multi-agent state management: shared contract, isolation, memory usage."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from researchpilot.agents.base import build_runtime
from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.observability.trace import current_tracer, tracer_scope
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest, ResearchResult, ResearchSettings


def test_cross_agent_state_contract_after_run(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """Planner → Researcher → Verifier → Writer must agree on every identifier."""
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=knowledge_base
    )
    result = pipeline.run(ResearchRequest(question="MCP 基于什么协议，核心方法有哪些？"))

    assert result.plan is not None
    subtask_ids = {subtask.id for subtask in result.plan.subtasks}
    evidence_ids = {item.id for item in result.evidence.evidence}
    source_ids = {source.id for source in result.evidence.sources}

    for item in result.evidence.evidence:
        assert item.subtask_id in subtask_ids, "evidence must point at a planned sub-task"
        assert item.source_id in source_ids, "evidence must point at a registered source"
    assert result.verification is not None
    for check in result.verification.checks:
        assert check.evidence_id in evidence_ids, "verification must reference real evidence"
    assert result.report is not None
    for claim in result.report.conclusions:
        assert set(claim.evidence_ids) <= evidence_ids
    for section in result.report.sections:
        assert set(section.evidence_ids) <= evidence_ids

    # State is structured and round-trippable, never a formatted string.
    assert isinstance(result.settings, ResearchSettings)
    assert "api_key" not in result.model_dump()
    round_tripped = ResearchResult.model_validate(json.loads(result.model_dump_json()))
    assert round_tripped.task_id == result.task_id
    assert len(round_tripped.evidence.evidence) == len(result.evidence.evidence)


def test_no_state_leak_between_concurrent_tasks(settings: Settings) -> None:
    """Two tasks running at the same time must not share tracers or evidence."""
    questions = [
        "MCP 的核心方法有哪些？",
        "长期记忆需要哪些字段和 TTL 策略？",
    ]
    results: dict[str, ResearchResult] = {}
    errors: list[BaseException] = []

    def worker(question: str) -> None:
        try:
            kb = KnowledgeBase(settings)
            kb.ingest_path(Path(settings.kb_path))
            pipeline = ResearchPipeline(
                settings=settings, provider=MockLLMProvider(settings), knowledge_base=kb
            )
            results[question] = pipeline.run(ResearchRequest(question=question))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(question,)) for question in questions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    assert len(results) == 2
    task_ids = {result.task_id for result in results.values()}
    assert len(task_ids) == 2, "each task must get its own task_id"
    for question, result in results.items():
        assert result.question == question
        if result.plan:
            assert question in result.plan.objective
        trace = result.trace_id
        assert trace.startswith("trace_")
    # No ambient tracer survives outside a run.
    assert current_tracer() is None


def test_tracer_scope_is_context_local(settings: Settings) -> None:
    from researchpilot.observability.trace import Tracer

    tracer = Tracer("scope-task", "q")
    assert current_tracer() is None
    with tracer_scope(tracer):
        assert current_tracer() is tracer
    assert current_tracer() is None


def test_memory_layers_are_actually_used(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """Memory must be written and read by the agents, not merely defined."""
    runtime = build_runtime(
        task_id="memory-task",
        question="MCP 与工具权限",
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
    )
    pipeline = ResearchPipeline(
        settings=settings, provider=MockLLMProvider(settings), knowledge_base=knowledge_base
    )
    result = pipeline.run(ResearchRequest(question="MCP 与工具权限的设计要点是什么？"))

    long_term = settings.runs_dir() / "long_term_memory.json"
    assert long_term.exists(), "long-term memory must be persisted after a task"
    payload = json.loads(long_term.read_text(encoding="utf-8"))
    assert payload["records"], "the task topic/conclusions must be stored"
    assert any("研究主题" in record["content"] for record in payload["records"])

    # fresh runtime keeps its own short-term/working state and reads long-term memory
    memory_context = runtime.memory.recall_context(result.question, limit=3)
    assert isinstance(memory_context, str)
    assert result.metrics.usage.total_tokens > 0
