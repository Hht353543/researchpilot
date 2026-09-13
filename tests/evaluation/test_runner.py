"""Evaluation runner mechanics: metrics, judgements, fault injection, persistence."""

from __future__ import annotations

from pathlib import Path

from researchpilot.config import Settings
from researchpilot.evaluation.runner import EvaluationRunner, load_report
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.rag.knowledge_base import KnowledgeBase

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _runner(settings: Settings, tmp_path: Path) -> EvaluationRunner:
    kb = KnowledgeBase(settings)
    kb.ingest_path(FIXTURES / "kb")
    return EvaluationRunner(
        dataset_path=FIXTURES / "golden_subset.jsonl",
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=kb,
        output_dir=tmp_path / "benchmarks",
    )


def test_runner_produces_metrics_from_real_runs(settings: Settings, tmp_path: Path) -> None:
    report = _runner(settings, tmp_path).run()
    assert report.metrics.tasks == 3
    assert report.metrics.passed >= 2
    assert 0.0 <= report.metrics.task_success_rate <= 1.0
    assert report.metrics.retrieval_recall > 0
    assert report.metrics.citation_correctness > 0
    assert report.metrics.tool_calls > 0
    assert report.metrics.total_tokens > 0
    assert report.metrics.avg_latency_s > 0
    assert report.provider == "mock"
    assert "offline" in report.mode
    assert report.dataset["tasks"] >= 3
    assert report.categories


def test_runner_persists_reports(settings: Settings, tmp_path: Path) -> None:
    runner = _runner(settings, tmp_path)
    report = runner.run()
    files = list((tmp_path / "benchmarks").glob("*.json"))
    assert files
    reloaded = load_report(tmp_path / "benchmarks" / f"latest_{report.provider}.json")
    assert reloaded.metrics.tasks == report.metrics.tasks
    assert reloaded.judgements[0].task_id == report.judgements[0].task_id


def test_runner_can_filter_tasks(settings: Settings, tmp_path: Path) -> None:
    runner = _runner(settings, tmp_path)
    report = runner.run(task_ids=["fx-01"])
    assert report.metrics.tasks == 1
    assert report.judgements[0].task_id == "fx-01"


def test_judgement_records_checks(settings: Settings, tmp_path: Path) -> None:
    report = _runner(settings, tmp_path).run()
    judgement = next(j for j in report.judgements if j.task_id == "fx-01")
    names = {check.name for check in judgement.checks}
    assert "report_present" in names
    assert any(name.startswith("required_any") for name in names)
    assert judgement.retrieved_docs
    assert judgement.used_tools
    assert judgement.latency_s > 0


def test_fault_injection_task_reports_tool_failure(tmp_path: Path) -> None:
    from researchpilot.evaluation.dataset import Expectations, GoldenTask

    task = GoldenTask(
        id="fx-fault",
        category="timeout_recovery",
        question="检索知识库说明混合检索与重排的作用。",
        expected_tools=["knowledge_search"],
        expectations=Expectations(min_tool_failures=1, status_in=["degraded", "failed", "succeeded"]),
        setup={
            "fault": {
                "tool": "knowledge_search",
                "mode": "timeout",
                "delay_s": 1.0,
                "timeout_s": 0.2,
                "max_retries": 1,
            }
        },
    )
    settings = Settings(
        provider="mock",
        kb_path=str(FIXTURES / "kb"),
        runs_path=str(tmp_path / "runs"),
        embedding_dim=64,
        top_k=3,
        web_search_mode="offline",
    )
    kb = KnowledgeBase(settings)
    kb.ingest_path(FIXTURES / "kb")
    runner = EvaluationRunner(
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=kb,
        output_dir=tmp_path / "benchmarks",
    )
    report = runner.run(tasks=[task], persist=False)
    judgement = report.judgements[0]
    assert judgement.tool_failures >= 1
    assert any(check.name == "tool_failures_observed" for check in judgement.checks)
    assert judgement.status in {"degraded", "failed", "succeeded"}
