"""Generated docs must describe the provider that actually produced them.

The resume generator hardcoded「上表来自离线确定性 provider（mock）」, so a live
model run produced a resume that named the real model in one line and claimed the
numbers came from mock in another.
"""

from __future__ import annotations

from researchpilot.evaluation.report import render_evaluation_markdown, render_resume_section
from researchpilot.evaluation.runner import EvaluationReport

MOCK_RESUME_CLAIM = "离线确定性 provider（mock）"
MOCK_EVAL_CLAIM = "Mock provider 是确定性脚本模型"


def _report(provider: str, mode: str, model: str) -> EvaluationReport:
    return EvaluationReport(
        run_id=f"eval_{provider}",
        generated_at="2026-09-15T00:00:00Z",
        provider=provider,
        model=model,
        mode=mode,
        dataset_path="eval/golden_dataset.jsonl",
    )


def test_mock_run_says_the_numbers_come_from_mock() -> None:
    report = _report("mock", "offline", "mock-research-model")
    assert MOCK_RESUME_CLAIM in render_resume_section(report)
    assert MOCK_EVAL_CLAIM in render_evaluation_markdown(report)


def test_live_run_never_claims_the_numbers_are_from_mock() -> None:
    report = _report("openai", "live model", "deepseek-chat")
    resume = render_resume_section(report)
    evaluation = render_evaluation_markdown(report)
    assert MOCK_RESUME_CLAIM not in resume, resume
    assert MOCK_EVAL_CLAIM not in resume
    assert "deepseek-chat" in resume
    assert "真实模型调用" in resume
    assert MOCK_EVAL_CLAIM not in evaluation
    assert "真实模型调用" in evaluation


def test_live_run_does_not_call_its_cost_free() -> None:
    """The cost row described every run as a free offline mock run."""
    report = _report("openai", "live model", "deepseek-chat")
    report.metrics.total_cost_usd = 1.164265
    report.metrics.avg_cost_usd = 0.033265
    evaluation = render_evaluation_markdown(report)
    assert "$0.033265" in evaluation
    assert "离线 mock provider 成本为 0" not in evaluation
    assert "按 `configs/pricing.yaml` 计价" in evaluation


def test_mock_run_still_says_its_cost_is_zero() -> None:
    report = _report("mock", "offline", "mock-research-model")
    evaluation = render_evaluation_markdown(report)
    assert "离线 mock provider 成本为 0" in evaluation


def test_writer_fallback_count_is_visible_in_both_documents() -> None:
    """The confound the live run exposed must be readable in the artifacts."""
    report = _report("openai", "live model", "deepseek-chat")
    report.metrics.tasks = 35
    report.metrics.writer_fallback_tasks = 24
    evaluation = render_evaluation_markdown(report)
    resume = render_resume_section(report)
    assert "Writer Fallback Reports | 24 / 35" in evaluation
    assert "24 / 35 条报告由确定性回退写作器产出" in resume


def test_mock_run_does_not_mention_the_fallback_writer() -> None:
    report = _report("mock", "offline", "mock-research-model")
    report.metrics.tasks = 35
    assert "回退写作器" not in render_resume_section(report)


def test_metrics_count_the_fallback_reports() -> None:
    from researchpilot.agents.writer import WRITER_FALLBACK_MARKER
    from researchpilot.evaluation.judge import TaskJudgement
    from researchpilot.evaluation.metrics import aggregate

    judgements = [
        TaskJudgement(
            task_id="a",
            category="rag",
            passed=True,
            status="degraded",
            errors=[f"writer: x; {WRITER_FALLBACK_MARKER}"],
        ),
        TaskJudgement(task_id="b", category="rag", passed=True, status="succeeded", errors=[]),
        TaskJudgement(
            task_id="c",
            category="rag",
            passed=False,
            status="degraded",
            errors=["tool: TimeoutError: slow"],
        ),
    ]
    assert aggregate(judgements).writer_fallback_tasks == 1
