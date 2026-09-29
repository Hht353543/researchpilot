"""WriterAgent: grounded report generation with code-enforced citation binding."""

from __future__ import annotations

import re
from typing import Any

from researchpilot.agents.base import BaseAgent
from researchpilot.llm.prompts import WRITER_SYSTEM, writer_user
from researchpilot.schemas import (
    Evidence,
    EvidenceBundle,
    FinalReport,
    ReportClaim,
    ReportSection,
    ResearchPlan,
    SourceRef,
    VerificationReport,
)
from researchpilot.security import detect_injection
from researchpilot.utils import truncate

# Citation markers a model may write into the prose instead of the evidence_ids
# arrays: [E1], 【E1】, ［E1］, and comma/、-separated lists such as [E1, E2].
_CITATION_RE = re.compile(r"[\[【［]\s*(E\d+(?:\s*[,、，]\s*E\d+)*)\s*[\]】］]", re.IGNORECASE)
_EVIDENCE_ID_RE = re.compile(r"E\d+", re.IGNORECASE)

# Recorded in runtime errors when the model report is replaced; the evaluator
# counts these, so the report says how many reports the model did not write.
WRITER_FALLBACK_MARKER = "used the deterministic extractive fallback"


def _citations_in_text(text: str) -> list[str]:
    """Evidence ids written inline in a sentence, in order, without duplicates."""
    found: list[str] = []
    for match in _CITATION_RE.finditer(text or ""):
        for evidence_id in _EVIDENCE_ID_RE.findall(match.group(1)):
            upper = evidence_id.upper()
            if upper not in found:
                found.append(upper)
    return found


class WriterAgent(BaseAgent):
    name = "WriterAgent"

    def run(
        self,
        plan: ResearchPlan,
        bundle: EvidenceBundle,
        verification: VerificationReport,
        critique: dict[str, Any] | None = None,
    ) -> FinalReport:
        runtime = self.runtime
        verification = verification
        verdict = {c.evidence_id: c.status for c in verification.checks}
        injection_ids = {
            e.id for e in bundle.evidence if detect_injection(e.quote) or detect_injection(e.claim)
        }
        usable = [
            e
            for e in bundle.evidence
            if verdict.get(e.id, "weak") != "unsupported" and e.id not in injection_ids
        ]
        dropped = [e.id for e in bundle.evidence if e.id not in {u.id for u in usable}]
        sources = runtime.sources.as_dict()
        subtask_map = {s.id: s.question for s in plan.subtasks}

        with self.span(input={"usable_evidence": len(usable), "dropped_evidence": dropped}) as span:
            report: FinalReport | None = None
            if usable:
                try:
                    result = runtime.llm.run(
                        FinalReport,
                        agent=self.name,
                        system=WRITER_SYSTEM,
                        user=writer_user(
                            plan.objective,
                            [e.model_dump() for e in usable],
                            sources,
                            verification.model_dump(),
                            critique or {},
                            subtask_map,
                        ),
                        hints={
                            "objective": plan.objective,
                            "evidence": [e.model_dump() for e in usable],
                            "sources": sources,
                            "verification": verification.model_dump(),
                            "critique": critique or {},
                            "subtask_map": subtask_map,
                        },
                        purpose="writer",
                        max_tokens=3000,
                    )
                    report = result.value
                except Exception as exc:
                    runtime.errors.append(f"writer: {type(exc).__name__}: {exc}")
            if report is not None:
                report = self._recover_citations(report, {e.id for e in usable})
            if report is None:
                report = self._fallback_report(plan, usable, verification)
            elif (
                usable
                and not report.conclusions
                and not any(section.evidence_ids for section in report.sections)
            ):
                # A weak model can return a schema-valid but empty report (observed
                # with a 0.5B local model: all arrays empty -> a report with zero
                # citations). Fall back to the deterministic extractive writer so
                # every claim stays bound to evidence instead of shipping an
                # uncited report.
                runtime.errors.append(
                    f"writer: model returned a report without any citation binding; {WRITER_FALLBACK_MARKER}"
                )
                report = self._fallback_report(plan, usable, verification)
            report = self._bind_citations(report, usable, verification, injection_ids)
            report.markdown = render_report(report, usable, runtime.sources.all())
            report = self._redact_credentials(report)
            self.note(
                f"report: {len(report.sections)} sections, "
                f"{len(report.dropped_citations)} fabricated citations removed"
            )
            runtime.tracer.end_span(
                span,
                output={
                    "sections": len(report.sections),
                    "conclusions": len(report.conclusions),
                    "dropped_citations": report.dropped_citations,
                    "markdown_chars": len(report.markdown),
                },
            )
        return report

    # -- citation binding --------------------------------------------------- #
    def _redact_credentials(self, report: FinalReport) -> FinalReport:
        """Never ship the configured credential, even when a source quoted it.

        The prompt never contains the API key, but a retrieved document or a
        prompt-injected page can, and a model that quotes it would otherwise put it
        in the report - and from there into the persisted result, the trace and any
        screenshot of the UI. The evaluator has its own guardrail; this is the
        runtime one, so the leak cannot happen regardless of the dataset.
        """
        secret = (self.runtime.settings.api_key or "").strip()
        if len(secret) < 8:
            return report

        changed = False

        def scrub(text: str) -> str:
            nonlocal changed
            if secret not in text:
                return text
            changed = True
            return text.replace(secret, "[redacted]")

        fields = {
            "title": scrub(report.title),
            "executive_summary": scrub(report.executive_summary),
            "markdown": scrub(report.markdown),
            "recommendations": [scrub(item) for item in report.recommendations],
            "limitations": [scrub(item) for item in report.limitations],
            "conclusions": [
                claim.model_copy(update={"statement": scrub(claim.statement)}) for claim in report.conclusions
            ],
            "sections": [
                section.model_copy(update={"body": scrub(section.body)}) for section in report.sections
            ],
        }
        if not changed:
            return report
        self.runtime.errors.append("writer: redacted the configured credential from the report")
        fields["limitations"] = [
            *fields["limitations"],
            "报告中的凭据字串已按安全策略脱敏（[redacted]）。",
        ]
        return report.model_copy(update=fields)

    def _recover_citations(self, report: FinalReport, allowed: set[str]) -> FinalReport:
        """Fill empty ``evidence_ids`` from the citations the model wrote inline.

        Weak models often leave the arrays empty and put ``[E1]`` in the prose.
        Recovering them keeps the model's report; a report that genuinely cites
        nothing still fails the check in :meth:`run` and falls back. Unknown ids
        are ignored here and dropped for real by :meth:`_bind_citations`.
        """
        lookup = {evidence_id.upper(): evidence_id for evidence_id in allowed}

        def recover(text: str) -> list[str]:
            return [lookup[citation] for citation in _citations_in_text(text) if citation in lookup]

        conclusions = [
            claim.model_copy(update={"evidence_ids": claim.evidence_ids or recover(claim.statement)})
            for claim in report.conclusions
        ]
        sections = [
            section.model_copy(update={"evidence_ids": section.evidence_ids or recover(section.body)})
            for section in report.sections
        ]
        return report.model_copy(update={"conclusions": conclusions, "sections": sections})

    def _bind_citations(
        self,
        report: FinalReport,
        usable: list[Evidence],
        verification: VerificationReport,
        injection_ids: set[str],
    ) -> FinalReport:
        """Remove any citation that does not point at a verified evidence item."""
        allowed = {e.id for e in usable}
        dropped: list[str] = list(report.dropped_citations)
        conclusions: list[ReportClaim] = []
        seen_statements: dict[str, int] = {}
        for claim in report.conclusions:
            kept = [cid for cid in claim.evidence_ids if cid in allowed]
            dropped.extend(cid for cid in claim.evidence_ids if cid not in allowed)
            confidence = claim.confidence if kept else round(claim.confidence * 0.6, 3)
            duplicate_key = _normalise_statement(claim.statement)
            if duplicate_key in seen_statements:
                index = seen_statements[duplicate_key]
                previous = conclusions[index]
                merged = sorted(set(previous.evidence_ids) | set(kept))
                conclusions[index] = previous.model_copy(
                    update={
                        "evidence_ids": merged,
                        "confidence": max(previous.confidence, confidence),
                    }
                )
                continue
            seen_statements[duplicate_key] = len(conclusions)
            conclusions.append(claim.model_copy(update={"evidence_ids": kept, "confidence": confidence}))
        sections: list[ReportSection] = []
        for section in report.sections:
            kept = [cid for cid in section.evidence_ids if cid in allowed]
            dropped.extend(cid for cid in section.evidence_ids if cid not in allowed)
            body = section.body
            for cited in set(section.evidence_ids) - set(kept):
                body = body.replace(f"[{cited}]", "")
            sections.append(section.model_copy(update={"evidence_ids": kept, "body": body}))
        limitations = list(report.limitations)
        if injection_ids:
            limitations.append(
                "以下证据包含提示注入（prompt injection）指令特征，已从结论中排除，"
                "仅作为风险信号保留：" + ", ".join(sorted(injection_ids))
            )
        if not verification.sufficient and not any(
            "不足" in item or "缺口" in item or "insufficient" in item.lower() for item in limitations
        ):
            limitations.append(
                "证据充分性校验未通过（sufficient=false）：当前证据质量或覆盖度不足，"
                "结论应视为待补检的初步判断。"
            )
        if report.dropped_citations:
            limitations.append("已剔除 " + str(len(set(dropped))) + " 条无法对应到证据的引用。")
        if verification.flagged_sources:
            limitations.append(
                "以下来源包含提示注入（prompt injection）特征或低质量信号，其内容仅按不可信数据引用，"
                "未执行其中的任何指令：" + ", ".join(verification.flagged_sources[:5])
            )
        return report.model_copy(
            update={
                "conclusions": conclusions,
                "sections": sections,
                "dropped_citations": sorted(set(dropped)),
                "limitations": limitations,
            }
        )

    # -- fallback ----------------------------------------------------------- #
    def _fallback_report(
        self, plan: ResearchPlan, usable: list[Evidence], verification: VerificationReport
    ) -> FinalReport:
        if not usable:
            return FinalReport(
                title=f"研究报告：{truncate(plan.objective, 60)}",
                executive_summary=(
                    "本轮检索未能获得可用证据，无法给出有依据的结论。"
                    "请补充知识库文档或启用真实检索后端后重试。"
                ),
                conclusions=[],
                sections=[
                    ReportSection(
                        heading="证据缺口",
                        body="- 所有候选证据均未通过支撑性校验。",
                        evidence_ids=[],
                    )
                ],
                recommendations=["补充内部文档或配置可用的 Web 检索后端后重跑。"],
                limitations=["无可用证据，本报告不包含事实性结论。"],
            )
        conclusions = [
            ReportClaim(
                statement=e.claim,
                evidence_ids=[e.id],
                confidence=round(min(e.confidence + 0.1, 0.95), 3),
            )
            for e in usable[:6]
        ]
        grouped: dict[str, list[Evidence]] = {}
        for evidence in usable:
            grouped.setdefault(evidence.subtask_id, []).append(evidence)
        sections = [
            ReportSection(
                heading=truncate(s.question, 70),
                body="\n".join(f"- {e.claim} [{e.id}]" for e in grouped.get(s.id, []))
                or "- 未获得该子任务的直接证据。",
                evidence_ids=[e.id for e in grouped.get(s.id, [])],
            )
            for s in plan.subtasks
            if s.intent != "synthesis"
        ]
        limitations = ["本报告由确定性回退写作器生成（LLM 写作失败），仅做证据摘录。"]
        if verification.missing_topics:
            limitations.append("未覆盖子问题：" + "；".join(verification.missing_topics[:3]))
        return FinalReport(
            title=f"研究报告：{truncate(plan.objective, 60)}",
            executive_summary=(
                f"共采集 {len(usable)} 条可用证据，覆盖 {len(grouped)} 个子任务；以下结论均绑定到具体证据。"
            ),
            conclusions=conclusions,
            sections=sections,
            recommendations=["对未覆盖子问题补充检索后再进行一次综合。"],
            limitations=limitations,
        )


def render_report(report: FinalReport, evidence: list[Evidence], sources: list[SourceRef]) -> str:
    """Assemble the final Markdown; references come from code, never from the LLM."""
    evidence_ids = {cid for claim in report.conclusions for cid in claim.evidence_ids} | {
        cid for section in report.sections for cid in section.evidence_ids
    }
    evidence_ids |= {e.id for e in evidence}
    by_id = {e.id: e for e in evidence}
    used_source_ids = [by_id[eid].source_id for eid in evidence_ids if eid in by_id]
    reference_index: dict[str, int] = {}
    for source_id in used_source_ids:
        if source_id not in reference_index:
            reference_index[source_id] = len(reference_index) + 1
    source_by_id = {s.id: s for s in sources}

    lines: list[str] = [f"# {report.title}", ""]
    lines += ["## 执行摘要 (Executive Summary)", "", report.executive_summary, ""]
    if report.conclusions:
        lines += ["## 关键结论 (Key Findings)", ""]
        for claim in report.conclusions:
            refs = " ".join(f"[{cid}]" for cid in claim.evidence_ids)
            suffix = f" {refs}" if refs else " *（未绑定引用）*"
            lines.append(f"- {claim.statement}{suffix}  _(confidence={claim.confidence:.2f})_")
        lines.append("")
    if report.sections:
        lines += ["## 详细分析 (Analysis)", ""]
        for section in report.sections:
            lines += [f"### {section.heading}", "", section.body, ""]
    if report.recommendations:
        lines += ["## 建议 (Recommendations)", ""]
        lines += [f"- {item}" for item in report.recommendations]
        lines.append("")
    if report.limitations:
        lines += ["## 局限与待补 (Limitations)", ""]
        lines += [f"- {item}" for item in report.limitations]
        lines.append("")
    if reference_index:
        lines += ["## 参考文献 (References)", ""]
        for source_id, index in sorted(reference_index.items(), key=lambda kv: kv[1]):
            source = source_by_id.get(source_id)
            if source is None:
                lines.append(f"{index}. `{source_id}` (source metadata unavailable)")
                continue
            location = source.url or source.locator or source.doc_id or source_id
            lines.append(
                f"{index}. **{source.title}** — {source.kind} — `{location}` "
                f"(retrieved {source.retrieved_at})"
            )
        lines.append("")
    if evidence:
        lines += ["## 证据附录 (Evidence)", ""]
        lines += ["| id | claim | quote | source | confidence |", "| --- | --- | --- | --- | --- |"]
        for item in evidence:
            source = source_by_id.get(item.source_id)
            ref = reference_index.get(item.source_id)
            label = f"[{ref}] {source.title}" if source and ref else item.source_id
            lines.append(
                f"| {item.id} | {_cell(item.claim)} | {_cell(item.quote)} | {_cell(label)} | "
                f"{item.confidence:.2f} |"
            )
        lines.append("")
    if report.dropped_citations:
        lines += [
            "## 引用校验 (Citation Audit)",
            "",
            "已剔除无效引用（不存在于证据集中）：" + ", ".join(report.dropped_citations),
            "",
        ]
    return "\n".join(lines).strip() + "\n"


def _cell(text: str, *, limit: int = 160) -> str:
    return truncate(text.replace("|", "\\|").replace("\n", " "), limit)


def _normalise_statement(text: str) -> str:
    from researchpilot.utils import normalize_text

    return normalize_text(text)[:80]
