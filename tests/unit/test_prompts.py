"""Prompt engineering contracts: untrusted blocks are JSON and carry guardrails."""

from __future__ import annotations

import json
import re

from researchpilot.llm import prompts
from researchpilot.schemas import Evidence


def _block(prompt: str, label: str) -> str:
    match = re.search(rf'<untrusted source="{label}">\n(.*?)\n</untrusted>', prompt, re.S)
    assert match, f"missing untrusted block for {label}"
    return match.group(1)


def test_structured_payloads_are_embedded_as_json() -> None:
    evidence = [Evidence(id="E1", claim="他说：'ok'", quote="引用文本", source_id="s1")]
    prompt = prompts.writer_user(
        "目标",
        [item.model_dump() for item in evidence],
        {"s1": {"kind": "web", "score": 0.5}},
        {"sufficient": True},
        {"issues": []},
        {"S1": "子问题"},
    )
    payload = json.loads(_block(prompt, "evidence"))
    assert payload[0]["id"] == "E1"
    assert payload[0]["claim"] == "他说：'ok'"
    assert isinstance(json.loads(_block(prompt, "sources")), dict)


def test_every_agent_system_prompt_contains_safety_rules() -> None:
    system_prompts = {
        "planner": prompts.PLANNER_SYSTEM,
        "researcher": prompts.RESEARCHER_SYSTEM,
        "verifier": prompts.VERIFIER_SYSTEM,
        "critic": prompts.CRITIC_SYSTEM,
        "writer": prompts.WRITER_SYSTEM,
        "query_rewrite": prompts.QUERY_REWRITE_SYSTEM,
        "rerank": prompts.RERANK_SYSTEM,
    }
    for name, system in system_prompts.items():
        assert "<untrusted>" in system, f"{name} prompt lost the data/instruction rule"
        assert "Never invent facts" in system or "Never invent" in system
    assert prompts.PROMPT_VERSION


def test_planner_and_researcher_prompts_render_context() -> None:
    planner = prompts.planner_user(
        "研究问题",
        [{"name": "knowledge_search", "description": "检索", "permission": "read_only"}],
        {"documents": 3, "chunks": 10},
    )
    assert "knowledge_search" in planner
    assert _block(planner, "user-question") == "研究问题"

    researcher = prompts.researcher_user(
        "子问题",
        [{"tool": "knowledge_search", "rendered": "[kb#c1] 标题\n正文内容"}],
        ["gap-1"],
    )
    assert "[kb#c1]" in researcher
    assert "gap-1" in researcher
