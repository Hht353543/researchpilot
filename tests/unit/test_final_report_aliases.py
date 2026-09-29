"""A real model names the report's sections itself; the schema must cope.

Captured from the 2026-09-15 deepseek-chat run: the writer answered with
``findings[].narrative`` (its reading of the prompt's "findings" structure) while
the schema expects ``sections[].body``. Pydantic dropped the unknown keys, the
report validated with empty lists, and eight of ten reports were replaced by the
deterministic extractive writer - which made the citation numbers look better
while the model's own text was thrown away.
"""

from __future__ import annotations

from typing import Any

from researchpilot.agents.base import build_runtime
from researchpilot.agents.writer import WriterAgent
from researchpilot.llm.prompts import WRITER_SYSTEM
from researchpilot.schemas import (
    Evidence,
    EvidenceBundle,
    FinalReport,
    ResearchPlan,
    Subtask,
    VerificationReport,
)

FALLBACK_MARKER = "deterministic extractive fallback"

FINDINGS_PAYLOAD: dict[str, Any] = {
    "title": "报告标题",
    "executive_summary": "摘要 [E1]",
    "findings": [
        {
            "subtask_id": "S1",
            "heading": "分块策略",
            "narrative": "固定长度分块简单但会切断语义 [E1]",
            "evidence_ids": ["E1"],
            "confidence": 0.5,
        },
        {
            "subtask_id": "S2",
            "heading": "标题感知分块",
            "narrative": "标题感知分块保留章节路径，检索时命中更准 [E2]",
            "evidence_ids": ["E2"],
        },
    ],
    "recommendations": [],
    "limitations": [],
}


def test_findings_are_folded_into_sections() -> None:
    report = FinalReport.model_validate(FINDINGS_PAYLOAD)
    assert [section.heading for section in report.sections] == ["分块策略", "标题感知分块"]
    assert report.sections[0].evidence_ids == ["E1"]
    assert "切断语义" in report.sections[0].body
    assert "findings" not in report.model_dump()


def test_findings_become_conclusions_too() -> None:
    """Otherwise the rendered report has no executive conclusions at all."""
    report = FinalReport.model_validate(FINDINGS_PAYLOAD)
    assert len(report.conclusions) == 2
    assert report.conclusions[0].evidence_ids == ["E1"]
    assert "切断语义" in report.conclusions[0].statement
    assert report.conclusions[1].confidence == 0.5


def test_long_findings_are_truncated_for_the_conclusion_bullet() -> None:
    payload = {
        "title": "t",
        "executive_summary": "s",
        "findings": [{"heading": "h", "narrative": "很长的段落。" * 200, "evidence_ids": ["E1"]}],
    }
    report = FinalReport.model_validate(payload)
    assert len(report.conclusions[0].statement) <= 400
    assert report.conclusions[0].statement.endswith("…")


def test_explicit_sections_win_over_findings() -> None:
    payload = dict(FINDINGS_PAYLOAD)
    payload["sections"] = [{"heading": "已给出", "body": "正文 [E3]", "evidence_ids": ["E3"]}]
    report = FinalReport.model_validate(payload)
    assert [section.heading for section in report.sections] == ["已给出"]


def test_a_report_without_findings_is_untouched() -> None:
    payload = {"title": "t", "executive_summary": "s", "conclusions": [], "sections": []}
    assert FinalReport.model_validate(payload).sections == []


def test_writer_keeps_a_findings_shaped_report(settings: Any, knowledge_base: Any) -> None:
    """The eight-of-ten regression: the model's text must survive."""

    class _Provider:
        name = "stub"

        def __init__(self, settings: Any) -> None:
            self.settings = settings

        def model_name(self) -> str:
            return "stub-findings"

        def complete(self, messages: Any, **_: Any) -> Any:
            import json

            from researchpilot.llm.base import LLMResponse

            return LLMResponse(text=json.dumps(FINDINGS_PAYLOAD, ensure_ascii=False), model="stub")

    runtime = build_runtime(
        task_id="alias",
        question="heading-aware 分块和固定长度分块的区别",
        settings=settings,
        provider=_Provider(settings),  # type: ignore[arg-type]
        knowledge_base=knowledge_base,
    )
    plan = ResearchPlan(
        objective="q",
        subtasks=[
            Subtask(
                id="S1",
                question="q",
                intent="synthesis",
                tools=[],
                expected_output="o",
            )
        ],
    )
    bundle = EvidenceBundle(
        evidence=[
            Evidence(id="E1", subtask_id="S1", claim="c", quote="q", source_id="s"),
            Evidence(id="E2", subtask_id="S1", claim="c", quote="q", source_id="s"),
        ]
    )
    report = WriterAgent(runtime).run(plan, bundle, VerificationReport(sufficient=True))

    assert not [error for error in runtime.errors if FALLBACK_MARKER in error]
    assert "切断语义" in report.markdown
    assert [section.evidence_ids for section in report.sections] == [["E1"], ["E2"]]


def test_writer_prompt_names_the_schema_fields() -> None:
    """The prompt must not tell the model to write a structure the schema lacks."""
    assert "findings" not in WRITER_SYSTEM
    for field in ("sections", "conclusions", "recommendations", "limitations", "evidence_ids"):
        assert field in WRITER_SYSTEM
