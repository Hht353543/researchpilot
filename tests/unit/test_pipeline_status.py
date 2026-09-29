"""Quality semantics for completed runs that finish without usable evidence.

A live deepseek-chat run failed the three ``no_result`` tasks with
``status=failed`` while the dataset expects ``succeeded|degraded``. The pipeline
returned ``failed`` whenever zero evidence came back together with any recorded
error, even though the run finished and produced a report - and the docstring it
sat under already said zero evidence is a degraded outcome.
"""

from __future__ import annotations

import threading

import pytest

from researchpilot.agents.base import build_runtime
from researchpilot.config import Settings
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.pipeline import CapacityExceededError, ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import Evidence, EvidenceBundle, ResearchRequest


def _runtime(settings: Settings, knowledge_base: KnowledgeBase):
    return build_runtime(
        task_id="status-semantics",
        question="查一个知识库里不存在的代号",
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
    )


def _evidence() -> Evidence:
    return Evidence(id="E1", subtask_id="S1", claim="c", quote="q", source_id="kb-1")


def test_no_evidence_with_recorded_errors_is_degraded(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    """A finished run that found nothing is degraded, not failed."""
    runtime = _runtime(settings, knowledge_base)
    runtime.errors.append("planner: ValidationError: the model rewrote the plan")
    assert ResearchPipeline._quality(runtime, EvidenceBundle()) == "degraded"


def test_evidence_with_recorded_errors_is_degraded(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    runtime = _runtime(settings, knowledge_base)
    runtime.errors.append("tool: TimeoutError: web_search exceeded 10s")
    bundle = EvidenceBundle(evidence=[_evidence()])
    assert ResearchPipeline._quality(runtime, bundle) == "degraded"


def test_clean_run_with_evidence_is_succeeded(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    runtime = _runtime(settings, knowledge_base)
    assert ResearchPipeline._quality(runtime, EvidenceBundle(evidence=[_evidence()])) == "succeeded"


@pytest.mark.parametrize("errors", [[], ["writer: StructuredOutputError: bad json"]])
def test_empty_run_is_never_succeeded(
    settings: Settings, knowledge_base: KnowledgeBase, errors: list[str]
) -> None:
    runtime = _runtime(settings, knowledge_base)
    runtime.errors.extend(errors)
    assert ResearchPipeline._quality(runtime, EvidenceBundle()) == "degraded"


def test_no_result_tasks_expect_a_degraded_outcome() -> None:
    """The dataset that exposed this: no_result tasks accept succeeded|degraded only."""
    import json
    from pathlib import Path

    dataset = Path(__file__).resolve().parents[2] / "eval" / "golden_dataset.jsonl"
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    no_result = [row for row in rows if row["category"] == "no_result"]
    assert no_result, "the dataset lost its no_result tasks"
    for row in no_result:
        assert set(row["expectations"]["status_in"]) == {"succeeded", "degraded"}


def test_pipeline_rejects_work_above_the_concurrent_task_limit(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    bounded = settings.model_copy(update={"max_concurrent_tasks": 1})
    pipeline = ResearchPipeline(
        settings=bounded,
        provider=MockLLMProvider(bounded),
        knowledge_base=knowledge_base,
    )
    started = threading.Event()
    release = threading.Event()
    completed: list[object] = []

    original_run = pipeline._run

    def slow_run(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=5)
        return original_run(*args, **kwargs)

    pipeline._run = slow_run  # type: ignore[method-assign]
    worker = threading.Thread(
        target=lambda: completed.append(pipeline.run(ResearchRequest(question="first task")))
    )
    worker.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(CapacityExceededError, match="at most 1"):
            pipeline.run(ResearchRequest(question="second task"))
    finally:
        release.set()
        worker.join(timeout=5)

    assert len(completed) == 1
    assert completed[0].status == "completed"  # type: ignore[union-attr]
    assert pipeline.run(ResearchRequest(question="third task")).status == "completed"
