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
