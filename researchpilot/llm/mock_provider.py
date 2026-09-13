"""Deterministic offline provider.

It is not a "fake": it is a scripted model used for tests, CI and reproducible
offline benchmarks. It consumes the same prompts as a real LLM (through the
``hints`` payload that mirrors the prompt content) and produces schema-valid
structured output, extractive evidence and grounded citations. Numbers produced
with this provider describe the *pipeline*, not the quality of a frontier model -
the evaluation reports label the provider explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import ChatMessage, LLMError, LLMProvider, LLMResponse
from researchpilot.llm.structured import estimated_usage
from researchpilot.schemas import (
    CitationCheck,
    CritiqueIssue,
    CritiqueReport,
    Evidence,
    EvidenceBundle,
    FinalReport,
    ReportClaim,
    ReportSection,
    RerankScore,
    RerankScores,
    ResearchPlan,
    Subtask,
    ToolName,
    VerificationReport,
)
from researchpilot.utils import (
    content_overlap,
    content_tokens,
    overlap_ratio,
    token_set,
    truncate,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[。；！？!?;])\s*|(?<=\.)\s+|\n+")
_PARAGRAPH_SPLIT = re.compile(r"\n{2,}|\n(?=\s*(?:[-*•]|\d+[.、])\s)")
_MAX_QUOTE_CHARS = 280


def split_sentences(text: str) -> list[str]:
    parts = [p.strip(" \t-•*") for p in _SENTENCE_SPLIT.split(text or "")]
    return [p for p in parts if len(p) >= 8]


def split_paragraphs(text: str) -> list[str]:
    """Paragraph / list-item level units used by the offline extractive baseline."""
    units: list[str] = []
    for raw in _PARAGRAPH_SPLIT.split(text or ""):
        lines = [line.strip() for line in raw.split("\n") if line.strip() and not _is_heading_path(line)]
        unit = "\n".join(lines).strip()
        if len(unit) < 12:
            continue
        units.append(unit)
    return units


def _is_heading_path(sentence: str) -> bool:
    """Chunk context lines (``section > subsection``) are not quotable evidence."""
    stripped = sentence.strip("# ").strip()
    raw = sentence.strip()
    return " > " in stripped or raw.startswith("|") or raw.startswith("#") or raw.startswith("---")


class MockLLMProvider(LLMProvider):
    """Scripted, deterministic provider (offline mode)."""

    name = "mock"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def model_name(self) -> str:
        return "mock-research-model"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[BaseModel] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> LLMResponse:
        hints = hints or {}
        prompt = "\n".join(m.content for m in messages)
        if response_schema is not None:
            handler = self._handlers().get(response_schema.__name__)
            if handler is None:
                raise LLMError(f"mock provider has no script for schema {response_schema.__name__}")
            value = handler(hints)
            text = value.model_dump_json(exclude_none=True)
        elif purpose == "query_rewrite":
            text = self._rewrite(hints)
        else:
            raise LLMError(f"mock provider has no script for purpose '{purpose}'")
        return LLMResponse(
            text=text,
            model=self.model_name(),
            usage=estimated_usage(prompt, text),
            finish_reason="stop",
            raw={"purpose": purpose, "provider": "mock"},
        )

    # -- scripts ------------------------------------------------------------ #
    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], BaseModel]]:
        return {
            "ResearchPlan": self._plan,
            "EvidenceBundle": self._evidence,
            "VerificationReport": self._verification,
            "CritiqueReport": self._critique,
            "FinalReport": self._report,
            "RerankScores": self._rerank,
        }

    def _plan(self, hints: dict[str, Any]) -> ResearchPlan:
        question: str = hints.get("question", "")
        available: list[str] = hints.get("tools") or [
            "knowledge_search",
            "web_search",
            "document_reader",
            "calculator",
            "metadata",
            "mcp_research_context",
        ]
        lowered = question.lower()

        def has(*words: str) -> bool:
            return any(w.lower() in lowered for w in words)

        subtasks: list[Subtask] = [
            Subtask(
                id="S1",
                question=f"{question} —— 先界定核心概念、范围与关键术语",
                intent="knowledge_search",
                tools=self._pick(available, "knowledge_search", fallback=True)
                + (self._pick(available, "mcp_research_context") if has("mcp", "网关", "gateway") else []),
                expected_output="核心概念定义、范围边界与术语表",
                priority=5,
            )
        ]
        if has("趋势", "现状", "发展", "市场", "trend", "adoption"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 梳理当前应用趋势与落地现状（含时间线与驱动因素）",
                    intent="web_search",
                    tools=self._pick(available, "web_search", "mcp_research_context", fallback=True),
                    expected_output="趋势时间线、驱动因素与可验证的市场信号",
                    priority=5,
                )
            )
        if has("技术路线", "架构", "方案", "实现", "技术栈", "route", "architecture"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 对比主要技术路线与实现方案",
                    intent="knowledge_search",
                    tools=self._pick(available, "knowledge_search", "document_reader", fallback=True),
                    expected_output="技术路线对比表（能力、复杂度、适用场景）",
                    priority=4,
                )
            )
        if has("项目", "案例", "代表", "product", "case", "framework"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 收集代表性项目/框架案例并提取关键事实",
                    intent="web_search",
                    tools=self._pick(available, "web_search", "knowledge_search", fallback=True),
                    expected_output="代表性项目清单及其定位、能力与限制",
                    priority=4,
                )
            )
        if has("优缺点", "风险", "挑战", "局限", "risk", "limitation", "trade-off"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 归纳优势、局限与风险，并给出权衡依据",
                    intent="knowledge_search",
                    tools=self._pick(available, "knowledge_search", "metadata", fallback=True),
                    expected_output="优缺点与风险的证据化清单",
                    priority=4,
                )
            )
        if has("参考", "来源", "资料", "文献", "reference", "source"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 汇总可追溯的参考资料与出处元数据",
                    intent="document_reader",
                    tools=self._pick(available, "document_reader", "metadata", fallback=True),
                    expected_output="带来源与元数据的参考资料列表",
                    priority=3,
                )
            )
        if re.search(r"\d+\s*[\+\-\*/×÷]", question) or has("计算", "估算", "比例"):
            subtasks.append(
                Subtask(
                    id=f"S{len(subtasks) + 1}",
                    question=f"{question} —— 对量化指标进行计算与校验",
                    intent="calculation",
                    tools=self._pick(available, "calculator"),
                    expected_output="计算结果与中间步骤",
                    priority=3,
                )
            )
        subtasks.append(
            Subtask(
                id=f"S{len(subtasks) + 1}",
                question=f"{question} —— 交叉验证证据并形成结论",
                intent="synthesis",
                tools=[],
                expected_output="经过验证的结论与引用绑定关系",
                priority=5,
                depends_on=[s.id for s in subtasks],
            )
        )
        return ResearchPlan(
            objective=question,
            subtasks=subtasks,
            requires_knowledge_base=True,
            requires_web=any(s.intent == "web_search" for s in subtasks),
            requires_mcp="mcp_research_context" in available,
            success_criteria=[
                "每个子任务都有可追溯证据或明确说明缺口",
                "结论句均绑定到具体证据 id",
                "引用来源均可追溯到知识库文档或搜索结果",
            ],
            max_iterations=hints.get("max_iterations", 2),
            rationale="按概念界定 → 事实检索 → 对比分析 → 风险权衡 → 交叉验证的顺序拆解。",
        )

    @staticmethod
    def _pick(available: list[str], *wanted: ToolName, fallback: bool = False) -> list[ToolName]:
        hit = [w for w in wanted if w in available]
        if hit or not fallback:
            return hit
        return ["knowledge_search"] if "knowledge_search" in available else []

    def _evidence(self, hints: dict[str, Any]) -> EvidenceBundle:
        subtask_question: str = hints.get("subtask_question", "")
        subtask_id: str = hints.get("subtask_id", "")
        observations: list[dict[str, Any]] = hints.get("observations") or []
        existing_ids: list[str] = hints.get("existing_evidence_ids") or []

        counter = len(existing_ids)
        evidence: list[Evidence] = []
        seen_quotes: set[str] = set()
        q_tokens = content_tokens(subtask_question)
        # One tool call = one evidence bucket. Items are ordered by a blend of retrieval
        # score and heading match, then allocated a paragraph quota (3/2/2/1/1...), which
        # keeps evidence balanced across sources instead of letting one chunk dominate.
        item_quotas = (3, 2, 2, 1, 1, 1, 1, 1)
        max_evidence = 8
        for observation in observations:
            if len(evidence) >= max_evidence:
                break
            tool = observation.get("tool", "")
            scored_items = [
                (
                    float(entry.get("score") or 0.0)
                    + 0.3
                    * content_overlap(
                        subtask_question,
                        f"{entry.get('section') or ''} {entry.get('title') or ''}",
                    ),
                    entry,
                )
                for entry in observation.get("items", [])
            ]
            scored_items.sort(key=lambda pair: pair[0], reverse=True)
            for index, (_, item) in enumerate(scored_items[: len(item_quotas)]):
                if len(evidence) >= max_evidence:
                    break
                quota = item_quotas[index]
                content = str(item.get("content") or "")
                source_id = str(item.get("source_id", ""))
                item_score = float(item.get("score") or 0.0)
                taken = 0
                for paragraph in split_paragraphs(content):
                    if taken >= quota or len(evidence) >= max_evidence:
                        break
                    key = paragraph[:80]
                    if key in seen_quotes:
                        continue
                    sentences = split_sentences(paragraph)
                    if not sentences:
                        continue
                    quote = paragraph[:_MAX_QUOTE_CHARS].strip()
                    tokens = content_tokens(quote)
                    dice = (2 * len(tokens & q_tokens)) / max(len(tokens) + len(q_tokens), 1)
                    relevance = min(0.5 * item_score + dice, 1.0)
                    seen_quotes.add(key)
                    taken += 1
                    counter += 1
                    evidence.append(
                        Evidence(
                            id=f"E{counter}",
                            subtask_id=subtask_id,
                            claim=self._claimify(sentences[0]),
                            quote=quote,
                            source_id=source_id,
                            tool=tool,
                            confidence=round(min(0.35 + relevance * 0.5, 0.95), 3),
                            relevance=round(max(min(relevance, 1.0), 0.0), 3),
                        )
                    )

        gaps: list[str] = []
        if not observations:
            gaps.append(f"{subtask_id or subtask_question}: 未获得任何工具观测结果")
        elif not evidence:
            gaps.append(f"{subtask_id or subtask_question}: 观测结果与子问题相关性不足")
        return EvidenceBundle(evidence=evidence, gaps=gaps, iterations=hints.get("iteration", 1))

    @staticmethod
    def _best_sentences(question: str, content: str, *, limit: int) -> list[str]:
        sentences = split_sentences(content)
        if not sentences:
            return []
        q_tokens = token_set(question)
        scored: list[tuple[float, int, str]] = []
        for idx, sentence in enumerate(sentences):
            tokens = token_set(sentence)
            if not tokens:
                continue
            inter = len(tokens & q_tokens)
            score = inter / max(len(q_tokens), 1) + min(len(sentence), 200) / 1000
            scored.append((score, -idx, sentence))
        scored.sort(reverse=True)
        picked = [s for score, _, s in scored if score > 0.02][:limit]
        if not picked:
            picked = [s for _, _, s in scored[:limit]]
        return picked

    @staticmethod
    def _claimify(sentence: str) -> str:
        text = sentence.strip().strip("-•* ")
        if len(text) <= 80:
            return text
        for sep in ("；", ";", "，", ",", "——"):
            head = text.split(sep)[0]
            if 12 <= len(head) <= 80:
                return head
        return truncate(text, 78)

    def _verification(self, hints: dict[str, Any]) -> VerificationReport:
        evidence: list[dict[str, Any]] = hints.get("evidence") or []
        sources: dict[str, dict[str, Any]] = hints.get("sources") or {}
        subtask_map: dict[str, str] = hints.get("subtask_map") or {}
        source_weights = {"knowledge_base": 0.85, "document": 0.8, "mcp": 0.75, "web": 0.65}

        checks: list[CitationCheck] = []
        quality: dict[str, float] = {}
        unsupported: list[str] = []
        covered: set[str] = set()
        for item in evidence:
            overlap = overlap_ratio(str(item.get("claim", "")), str(item.get("quote", "")))
            status: Literal["supported", "weak", "unsupported"] = (
                "supported" if overlap >= 0.6 else "weak" if overlap >= 0.3 else "unsupported"
            )
            if status == "unsupported":
                unsupported.append(str(item.get("claim", "")))
            elif status == "supported":
                covered.add(str(item.get("subtask_id", "")))
            source_id = str(item.get("source_id", ""))
            source = sources.get(source_id, {})
            kind = str(source.get("kind", "web"))
            score = source_weights.get(kind, 0.6)
            if source.get("title"):
                score = min(score + 0.05, 0.95)
            quality[source_id] = round(score, 3)
            checks.append(
                CitationCheck(
                    evidence_id=str(item.get("id", "")),
                    statement=str(item.get("claim", "")),
                    status=status,
                    reason=f"claim/quote token overlap = {overlap:.2f}; source={kind}",
                    overlap=round(overlap, 3),
                )
            )
        total_subtasks = max(len(subtask_map), 1)
        coverage = len({s for s in covered if s}) / total_subtasks
        unsupported_ratio = len(unsupported) / max(len(checks), 1)
        missing = [q for sid, q in subtask_map.items() if sid not in covered]
        mean_status = sum(
            {"supported": 1.0, "weak": 0.5, "unsupported": 0.0}[c.status] for c in checks
        ) / max(len(checks), 1)
        overall = round(0.5 * mean_status + 0.3 * coverage + 0.2 * (1 - unsupported_ratio), 3)
        return VerificationReport(
            checks=checks,
            unsupported_claims=unsupported,
            source_quality=quality,
            coverage_score=round(coverage, 3),
            sufficient=bool(checks) and coverage >= 0.5 and unsupported_ratio <= 0.34,
            missing_topics=missing,
            overall_score=overall,
        )

    def _critique(self, hints: dict[str, Any]) -> CritiqueReport:
        evidence: list[dict[str, Any]] = hints.get("evidence") or []
        verification: dict[str, Any] = hints.get("verification") or {}
        subtask_map: dict[str, str] = hints.get("subtask_map") or {}
        issues: list[CritiqueIssue] = []
        covered = {str(e.get("subtask_id", "")) for e in evidence if e.get("subtask_id")}
        missing = [q for sid, q in subtask_map.items() if sid not in covered]

        for question in missing[:4]:
            issues.append(
                CritiqueIssue(
                    severity="high",
                    category="coverage",
                    description=f"子任务缺少证据支撑：{truncate(question, 80)}",
                    suggested_fix="补充一次针对性检索（知识库优先，其次 Web）",
                )
            )
        if verification.get("unsupported_claims"):
            issues.append(
                CritiqueIssue(
                    severity="medium",
                    category="citation",
                    description=f"{len(verification['unsupported_claims'])} 条结论的引用支撑不足",
                    suggested_fix="删除或降级这些结论，或补充更强证据",
                )
            )
        duplicates = self._redundant_claims(evidence)
        if duplicates:
            issues.append(
                CritiqueIssue(
                    severity="low",
                    category="redundancy",
                    description=f"存在 {len(duplicates)} 组高度重复的证据表述",
                    suggested_fix="合并重复证据，保留来源更强的一条",
                )
            )
        if not any(e.get("source_id") and str(e.get("source_id", "")).startswith("web") for e in evidence):
            issues.append(
                CritiqueIssue(
                    severity="low",
                    category="recency",
                    description="证据集中在知识库，缺少外部时效性来源",
                    suggested_fix="补充一次 Web 检索以覆盖最新动态",
                )
            )
        coverage_ratio = len(covered & set(subtask_map)) / max(len(subtask_map), 1)
        return CritiqueReport(
            issues=issues,
            needs_more_research=bool(missing) or not verification.get("sufficient", False),
            follow_up_queries=[truncate(q, 120) for q in missing[:3]],
            coverage={
                "subtask_coverage": round(coverage_ratio, 3),
                "evidence_count": float(len(evidence)),
                "high_severity_issues": float(sum(1 for i in issues if i.severity == "high")),
            },
            overall_assessment=(
                "研究基本覆盖目标问题，仍存在上述缺口，建议按 follow-up 查询补检。"
                if issues
                else "覆盖度与引用强度达标，可以进入写作阶段。"
            ),
        )

    @staticmethod
    def _redundant_claims(evidence: list[dict[str, Any]]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for i, a in enumerate(evidence):
            for b in evidence[i + 1 :]:
                if overlap_ratio(str(a.get("claim", "")), str(b.get("claim", ""))) > 0.8:
                    pairs.append((str(a.get("id")), str(b.get("id"))))
        return pairs

    def _report(self, hints: dict[str, Any]) -> FinalReport:
        objective: str = hints.get("objective", "")
        evidence: list[dict[str, Any]] = hints.get("evidence") or []
        verification: dict[str, Any] = hints.get("verification") or {}
        critique: dict[str, Any] = hints.get("critique") or {}
        subtask_map: dict[str, str] = hints.get("subtask_map") or {}

        ranked = sorted(
            evidence,
            key=lambda e: (
                {"supported": 1.0, "weak": 0.5, "unsupported": 0.0}.get(
                    self._status_for(str(e.get("id", "")), verification), 0.0
                ),
                float(e.get("confidence", 0.0)),
            ),
            reverse=True,
        )
        usable = [e for e in ranked if self._status_for(str(e.get("id", "")), verification) != "unsupported"]

        summary_parts = [
            f"本研究围绕“{truncate(objective, 80)}”完成 {len(evidence)} 条证据采集，"
            f"其中 {len(usable)} 条通过支撑性校验。"
        ]
        if usable:
            summary_parts.append(
                "核心发现包括："
                + "；".join(f"{truncate(str(e['claim']), 60)} [{e['id']}]" for e in usable[:3])
                + "。"
            )
        if verification.get("missing_topics"):
            summary_parts.append(
                "证据仍存在缺口："
                + "；".join(truncate(str(t), 40) for t in verification["missing_topics"][:3])
                + "。"
            )

        conclusions = [
            ReportClaim(
                statement=str(e["claim"]),
                evidence_ids=[str(e["id"])],
                confidence=float(e.get("confidence", 0.5)),
            )
            for e in usable[:5]
        ]

        sections: list[ReportSection] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in usable:
            grouped.setdefault(str(item.get("subtask_id", "")), []).append(item)
        for sid, question in subtask_map.items():
            items = grouped.get(sid, [])
            if not items:
                sections.append(
                    ReportSection(
                        heading=truncate(question, 70),
                        body="- 该子任务未获得可引用证据，结论待补充（见 limitations）。",
                        evidence_ids=[],
                    )
                )
                continue
            body = "\n".join(
                f"- {truncate(str(i['claim']), 160)} [{i['id']}]（来源：{i.get('source_id', 'unknown')}）"
                for i in items
            )
            sections.append(
                ReportSection(
                    heading=truncate(question, 70),
                    body=body,
                    evidence_ids=[str(i["id"]) for i in items],
                )
            )

        recommendations: list[str] = []
        for topic in (verification.get("missing_topics") or [])[:3]:
            recommendations.append(f"针对“{truncate(str(topic), 60)}”补充一手资料后更新结论。")
        for issue in (critique.get("issues") or [])[:2]:
            fix = str(issue.get("suggested_fix") or "").strip()
            if fix:
                recommendations.append(fix)
        if not recommendations:
            recommendations.append("当前证据已能支撑主要结论，建议按季度复核时效性来源。")

        limitations: list[str] = []
        if verification.get("unsupported_claims"):
            limitations.append(
                f"有 {len(verification['unsupported_claims'])} 条候选结论因证据不足未纳入报告。"
            )
        if verification.get("missing_topics"):
            limitations.append("部分子问题缺少直接证据，相关结论以现有资料为限。")
        if not limitations:
            limitations.append("结论基于当前知识库与检索时间点的资料。")

        return FinalReport(
            title=f"研究报告：{truncate(objective, 60)}",
            executive_summary="".join(summary_parts),
            conclusions=conclusions,
            sections=sections,
            recommendations=recommendations,
            limitations=limitations,
        )

    @staticmethod
    def _status_for(evidence_id: str, verification: dict[str, Any]) -> str:
        for check in verification.get("checks") or []:
            if str(check.get("evidence_id")) == evidence_id:
                return str(check.get("status", "weak"))
        return "weak"

    def _rerank(self, hints: dict[str, Any]) -> RerankScores:
        query = str(hints.get("query", ""))
        passages: list[dict[str, Any]] = hints.get("passages") or []
        scores = []
        for passage in passages:
            text = str(passage.get("content", ""))
            overlap = overlap_ratio(query, text)
            lexical = len(token_set(query) & token_set(text)) / max(len(token_set(query)), 1)
            score = min(0.6 * lexical + 0.4 * overlap + float(passage.get("retrieval_score", 0.0)), 1.0)
            scores.append(
                RerankScore(
                    id=str(passage.get("id", "")),
                    score=round(min(max(score, 0.0), 1.0), 4),
                    reason=f"lexical overlap={lexical:.2f}",
                )
            )
        return RerankScores(scores=scores)

    def _rewrite(self, hints: dict[str, Any]) -> str:
        query = str(hints.get("query", "")).strip()
        topics = hints.get("known_topics") or []
        extras: list[str] = []
        for topic in topics[:3]:
            if topic and topic not in query and overlap_ratio(query, topic) < 0.5:
                extras.append(str(topic))
        expanded = query + (" " + " ".join(extras) if extras else "")
        return expanded.strip()
