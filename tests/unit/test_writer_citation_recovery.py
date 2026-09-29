"""The writer must use citations the model wrote in the prose.

The deepseek-chat baseline lost 24/35 reports to the extractive fallback writer.
One of the two triggers is independent of the planner: for `tool-03-calculator`
the shim evidence shows `writer_used_extractive_fallback: true` even after the
plan validated. A report that is schema-valid but leaves `evidence_ids` empty gets
thrown away wholesale, even when the model put `[E1]` markers in the text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from researchpilot.agents.base import build_runtime
from researchpilot.agents.writer import WriterAgent
from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMResponse
from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import (
    Evidence,
    EvidenceBundle,
    ResearchPlan,
    Subtask,
    VerificationReport,
)

FALLBACK_MARKER = "deterministic extractive fallback"


class _ReportProvider(MockLLMProvider):
    """Returns a canned FinalReport instead of the mock script."""

    def __init__(self, settings: Settings, payload: dict[str, Any]) -> None:
        super().__init__(settings)
        self.payload = payload

    def model_name(self) -> str:
        return "stub-report-model"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        **_: Any,
    ) -> LLMResponse:
        return LLMResponse(
            text=json.dumps(self.payload, ensure_ascii=False), model=self.model_name(), attempts=1
        )


def _report_payload(*, statement: str, body: str) -> dict[str, Any]:
    return {
        "title": "工具调用报告",
        "executive_summary": "本次计算与检索结果如下。",
        "conclusions": [{"statement": statement, "evidence_ids": [], "confidence": 0.7}],
        "sections": [{"heading": "计算", "body": body, "evidence_ids": []}],
        "recommendations": [],
        "limitations": [],
        "dropped_citations": [],
        "markdown": "",
    }


def _run_writer(
    settings: Settings, knowledge_base: KnowledgeBase, payload: dict[str, Any], evidence_ids: list[str]
):
    runtime = build_runtime(
        task_id="writer-recovery",
        question="计算 100 到 127 的增幅",
        settings=settings,
        provider=_ReportProvider(settings, payload),
        knowledge_base=knowledge_base,
    )
    plan = ResearchPlan(
        objective="计算 100 到 127 的增幅",
        subtasks=[
            Subtask(
                id="S1",
                question="计算结果",
                intent="calculation",
                tools=["calculator"],
                expected_output="数值",
            )
        ],
    )
    bundle = EvidenceBundle(
        evidence=[
            Evidence(id=eid, subtask_id="S1", claim="c", quote="q", source_id="src") for eid in evidence_ids
        ]
    )
    report = WriterAgent(runtime).run(plan, bundle, VerificationReport(sufficient=True))
    return runtime, report


def test_inline_citations_keep_the_model_report(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    payload = _report_payload(statement="增幅为 27% [E1]", body="计算得到 127，相对 100 增长 27%。")
    runtime, report = _run_writer(settings, knowledge_base, payload, ["E1"])

    assert report.title == "工具调用报告", "the model report must not be replaced"
    assert report.conclusions[0].evidence_ids == ["E1"]
    assert report.conclusions[0].statement.endswith("[E1]"), "prose must be preserved"
    assert not any(FALLBACK_MARKER in error for error in runtime.errors)


@pytest.mark.parametrize("marker", ["[E1, E2]", "【E1】和【E2】", "［E1、E2］"])
def test_full_width_and_list_markers_are_recovered(
    settings: Settings, knowledge_base: KnowledgeBase, marker: str
) -> None:
    payload = _report_payload(statement="结论", body=f"数据见 {marker}。")
    _, report = _run_writer(settings, knowledge_base, payload, ["E1", "E2"])
    assert report.sections[0].evidence_ids == ["E1", "E2"]


def test_lowercase_ids_match_the_registered_evidence(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    payload = _report_payload(statement="结论 [e1]", body="正文。")
    _, report = _run_writer(settings, knowledge_base, payload, ["E1"])
    assert report.conclusions[0].evidence_ids == ["E1"]


def test_empty_report_still_falls_back(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """No conclusions and no section binding is still an empty report."""
    payload = _report_payload(statement="", body="")
    payload["conclusions"] = []
    payload["sections"] = []
    runtime, report = _run_writer(settings, knowledge_base, payload, ["E1"])
    assert any(FALLBACK_MARKER in error for error in runtime.errors)
    assert report.title != "工具调用报告"


def test_unknown_inline_ids_are_not_a_binding(settings: Settings, knowledge_base: KnowledgeBase) -> None:
    """Citing an id that does not exist must not invent a binding."""
    payload = _report_payload(statement="结论 [E9]", body="正文 [E9]。")
    _, report = _run_writer(settings, knowledge_base, payload, ["E1"])
    assert report.conclusions[0].evidence_ids == []
    assert report.sections[0].evidence_ids == []
