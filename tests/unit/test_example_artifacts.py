"""The committed example artifacts are deliverables (§23) and must stay valid.

`examples/` is what a GitHub visitor opens first after the README, so a broken or
placeholder artifact would be worse than no artifact. These checks keep them
structurally sound and consistent with each other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
AGENTS = {"PlannerAgent", "ResearchAgent", "VerifierAgent", "CriticAgent", "WriterAgent"}


def _load(name: str) -> dict:
    path = EXAMPLES / name
    assert path.exists(), f"missing example artifact: {name}"
    assert path.stat().st_size > 1024, f"{name} looks like a placeholder"
    return json.loads(path.read_text(encoding="utf-8"))


def test_sample_result_is_a_complete_typed_run() -> None:
    result = _load("sample_result.json")
    for key in ("task_id", "trace_id", "status", "question", "plan", "evidence", "report", "metrics"):
        assert key in result, f"sample_result.json lacks {key}"
    assert result["status"] in {"succeeded", "degraded"}
    assert result["plan"]["subtasks"], "the sample plan must contain sub-tasks"
    evidence = result["evidence"]["evidence"]
    sources = {source["id"] for source in result["evidence"]["sources"]}
    assert evidence and sources
    evidence_ids = {item["id"] for item in evidence}
    for item in evidence:
        assert item["quote"].strip(), "evidence must carry verbatim quotes"
        assert item["source_id"] in sources, "evidence must point at a registered source"
    markdown = result["report"]["markdown"]
    cited = set(re.findall(r"\[(E\d+)\]", markdown))
    assert cited, "the sample report must contain citations"
    assert cited <= evidence_ids, "the sample report must not cite unknown evidence"
    assert "参考文献" in markdown and "证据附录" in markdown
    assert result["metrics"]["usage"]["total_tokens"] > 0
    assert result["metrics"]["llm_calls"] > 0 and result["metrics"]["tool_calls"] > 0


def test_sample_trace_covers_every_agent_and_span_kind() -> None:
    trace = _load("sample_trace.json")
    assert trace["spans"], "the sample trace must contain spans"
    kinds = {span["kind"] for span in trace["spans"]}
    assert {"agent", "llm", "tool", "retrieval"} <= kinds, f"missing span kinds: {kinds}"
    agents = {span["name"] for span in trace["spans"] if span["kind"] == "agent"}
    assert agents >= AGENTS, f"trace is missing agent spans: {AGENTS - agents}"
    assert all(span["start_time"] and span["end_time"] for span in trace["spans"])
    assert trace["metrics"]["usage"]["total_tokens"] > 0
    for span in trace["spans"]:
        if span["kind"] == "llm":
            assert span["model"], "llm spans must record the model"


def test_sample_report_markdown_matches_the_json_report() -> None:
    result = _load("sample_result.json")
    markdown = (EXAMPLES / "sample_report.md").read_text(encoding="utf-8")
    assert markdown.strip()
    assert markdown.startswith("# ")
    assert "参考文献" in markdown
    assert markdown.strip() == result["report"]["markdown"].strip(), (
        "sample_report.md must be the report stored in sample_result.json"
    )
