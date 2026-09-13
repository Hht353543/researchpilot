"""Evaluation smoke test: the full golden dataset must stay green in offline mode."""

from __future__ import annotations

import pytest

from researchpilot.evaluation.runner import EvaluationRunner


@pytest.mark.evaluation
def test_full_golden_dataset_offline_smoke() -> None:
    report = EvaluationRunner(provider_name="mock").run(persist=False)
    metrics = report.metrics

    assert metrics.tasks >= 30
    assert metrics.task_success_rate >= 0.8, report.failed_checks
    assert metrics.retrieval_recall >= 0.8
    assert metrics.citation_correctness >= 0.8
    assert metrics.tool_selection_accuracy >= 0.9
    assert metrics.avg_latency_s > 0
    assert metrics.total_tokens > 0

    # every judgement must carry evidence of a real run (not fabricated numbers)
    for judgement in report.judgements:
        assert judgement.latency_s > 0
        assert judgement.llm_calls >= 1
        assert judgement.checks
