"""WriterAgent: grounded report generation with code-enforced citation binding."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from researchpilot.agents.base import BaseAgent
from researchpilot.agents.support import calculation_numbers, clean_statement, direct_support, support_check
from researchpilot.llm.prompts import VERIFIER_SYSTEM, WRITER_SYSTEM, verifier_user, writer_user
from researchpilot.schemas import (
    CitationCheck,
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
            e for e in bundle.evidence if verdict.get(e.id) == "supported" and e.id not in injection_ids
        ]
        dropped = [e.id for e in bundle.evidence if e.id not in {u.id for u in usable}]
        sources = runtime.sources.as_dict()
        subtask_map = {s.id: s.question for s in plan.subtasks if s.intent != "synthesis"}

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
                        max_tokens=4000,
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
            report = self._bind_citations(
                report, usable, verification, injection_ids, include_diagnostics=False
            )
            publish = runtime.scratch.get("publish_stage")
            if publish:
                publish("checking_report")
            report = self._audit_report(report, usable, plan)
            report = self._add_diagnostics(report, verification, injection_ids)
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

    def _audit_report(self, report: FinalReport, usable: list[Evidence], plan: ResearchPlan) -> FinalReport:
        """Review the generated prose as well as the evidence extraction."""
        by_id = {e.id: e for e in usable}
        items: list[dict[str, Any]] = []

        def add(key: str, text: str, ids: list[str]) -> None:
            ids = [cid for cid in ids if cid in by_id]
            quote = "\n".join(by_id[cid].quote for cid in ids)
            context = "\n".join(
                dict.fromkeys(self.runtime.sources.content_of(by_id[cid].source_id) for cid in ids)
            )
            items.append(
                {
                    "id": key,
                    "claim": clean_statement(text),
                    "quote": quote,
                    "evidence_ids": ids,
                    "source_context": context or quote,
                    "kind": "advice"
                    if key.startswith("R")
                    else "limitation"
                    if key.startswith("L")
                    else "fact",
                    "arithmetic_checked": bool(calculation_numbers(text, quote)),
                }
            )

        for index, claim in enumerate(report.conclusions):
            add(f"C{index}", claim.statement, claim.evidence_ids)
        for index, section in enumerate(report.sections):
            add(f"S{index}", section.body, section.evidence_ids)
        if report.executive_summary:
            ids = _citations_in_text(report.executive_summary)
            if not ids:
                ids = list(dict.fromkeys(cid for c in report.conclusions for cid in c.evidence_ids))
            add("summary", report.executive_summary, ids)
        for index, text in enumerate(report.limitations):
            add(f"L{index}", text, list(by_id))
        for index, text in enumerate(report.recommendations):
            add(f"R{index}", text, _citations_in_text(text) or list(by_id))
        review = [
            item
            for item in items
            if item["quote"] and (item["kind"] != "fact" or not direct_support(item["claim"], item["quote"]))
        ]
        semantic: dict[str, CitationCheck] = {}
        if review and self.runtime.llm.provider.name != "mock":
            try:
                checked = self.runtime.llm.run(
                    VerificationReport,
                    agent=self.name,
                    system=VERIFIER_SYSTEM,
                    user=verifier_user(plan.objective, review, {}),
                    hints={"evidence": review},
                    purpose="report_verifier",
                    max_tokens=2500,
                ).value
                semantic = {check.evidence_id: check for check in checked.checks}
            except Exception as exc:
                self.runtime.errors.append(f"report_verifier: {type(exc).__name__}")
        checks = {
            item["id"]: support_check(
                item["id"],
                item["claim"],
                item["quote"],
                semantic.get(item["id"]),
                source_context=item["source_context"],
            )
            for item in items
        }

        # Empty evidence and mock runs retain their diagnostic text. With a real
        # model, advice and limitations must also pass review of factual premises.
        def keep_aux(key: str) -> bool:
            check = checks[key]
            return check.status == "supported" or (
                check.status == "weak" and (not usable or self.runtime.llm.provider.name == "mock")
            )

        limitations = [text for index, text in enumerate(report.limitations) if keep_aux(f"L{index}")]
        recommendations = [text for index, text in enumerate(report.recommendations) if keep_aux(f"R{index}")]
        rejected = [key for key, check in checks.items() if check.status != "supported"]
        if rejected:
            limitations.append("部分生成文字未能得到原文充分支持，已替换为可核对的原文摘录；推断请自行核实。")

        def extract(ids: list[str]) -> str:
            claims = _deduplicate_claims(
                [ReportClaim(statement=by_id[cid].quote, evidence_ids=[cid]) for cid in ids if cid in by_id]
            )
            return "\n".join(
                f"- {claim.statement} " + " ".join(f"[{cid}]" for cid in claim.evidence_ids)
                for claim in claims
            )

        covered_ids = {
            cid
            for index, claim in enumerate(report.conclusions)
            if checks[f"C{index}"].status == "supported"
            and not calculation_numbers(
                claim.statement, "\n".join(by_id[cid].quote for cid in claim.evidence_ids if cid in by_id)
            )
            for cid in claim.evidence_ids
        }
        conclusions = []
        for index, claim in enumerate(report.conclusions):
            check = checks[f"C{index}"]
            if check.status == "supported":
                quote = "\n".join(by_id[cid].quote for cid in claim.evidence_ids if cid in by_id)
                conclusions.append(
                    claim.model_copy(
                        update={
                            "assessment": "inference"
                            if calculation_numbers(claim.statement, quote)
                            else "supported",
                        }
                    )
                )
            elif claim.evidence_ids:
                # Ship the source wording instead of laundering the rejected claim.
                conclusions.extend(
                    ReportClaim(statement=by_id[cid].quote, evidence_ids=[cid], assessment="supported")
                    for cid in claim.evidence_ids
                    if cid in by_id and cid not in covered_ids
                )
            else:
                limitations.append("所选资料中没有充分证据支持部分生成结论。")
        conclusions = _deduplicate_claims(conclusions)
        sections = [
            section.model_copy(
                update={
                    "body": section.body
                    if checks[f"S{index}"].status == "supported"
                    else extract(section.evidence_ids) or "所选资料中没有充分证据回答这一部分。",
                }
            )
            for index, section in enumerate(report.sections)
        ]
        summary = report.executive_summary
        # Support alone does not establish completeness: an accurate income
        # summary can still omit the requested launch date. Preserve every
        # retained answer, including facts used only in an analysis section.
        summary_claims = list(conclusions)
        covered = {
            cid for claim in conclusions if claim.assessment != "inference" for cid in claim.evidence_ids
        }
        summary_claims.extend(
            ReportClaim(statement=by_id[cid].quote, evidence_ids=[cid])
            for section in sections
            for cid in section.evidence_ids
            if cid in by_id and cid not in covered
        )
        summary_claims = _deduplicate_claims(summary_claims)
        if summary_claims and (
            "summary" not in checks
            or checks["summary"].status != "supported"
            or any(key.startswith("C") for key in rejected)
            or any(not direct_support(claim.statement, summary) for claim in summary_claims)
        ):
            summary = (
                "\n".join(
                    f"{clean_statement(claim.statement)} "
                    + " ".join(f"[{cid}]" for cid in claim.evidence_ids)
                    for claim in summary_claims
                )
                or "所选资料中没有充分证据回答这个问题。"
            )
        final_checks = []
        for index, claim in enumerate(conclusions):
            quote = "\n".join(by_id[cid].quote for cid in claim.evidence_ids if cid in by_id)
            original = next(
                (c for c in checks.values() if c.statement == clean_statement(claim.statement)), None
            )
            final_checks.append(support_check(f"C{index}", clean_statement(claim.statement), quote, original))
        for index, section in enumerate(sections):
            quote = "\n".join(by_id[cid].quote for cid in section.evidence_ids if cid in by_id)
            if section.evidence_ids:
                final_checks.append(
                    support_check(f"S{index}", clean_statement(section.body), quote, checks[f"S{index}"])
                )
        return report.model_copy(
            update={
                "conclusions": conclusions,
                "sections": sections,
                "executive_summary": summary,
                "limitations": list(dict.fromkeys(limitations)),
                "recommendations": list(dict.fromkeys(recommendations)),
                "support_checks": final_checks,
                "rejected_claims": [checks[key] for key in rejected],
            }
        )

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
        *,
        include_diagnostics: bool = True,
    ) -> FinalReport:
        """Remove any citation that does not point at a verified evidence item."""
        allowed = {e.id for e in usable}
        # A chunk and its full document are the same source. Keep one citation
        # for identical original wording, while preserving distinct documents
        # and distinct quotes (especially conflicting or conditional claims).
        canonical: dict[tuple[str, str], str] = {}
        aliases: dict[str, str] = {}
        source_by_id = {source.id: source for source in self.runtime.sources.all()}
        for evidence in usable:
            source = source_by_id.get(evidence.source_id)
            if source is None:
                aliases[evidence.id] = evidence.id
                continue
            origin = f"doc:{source.doc_id}" if source and source.doc_id else evidence.source_id
            key = (origin, _normalise_statement(evidence.quote))
            aliases[evidence.id] = canonical.setdefault(key, evidence.id)

        def bind_ids(ids: list[str]) -> list[str]:
            return list(dict.fromkeys(aliases[cid] for cid in ids if cid in allowed))

        def bind_text(text: str) -> str:
            def replace(match: re.Match[str]) -> str:
                ids = _EVIDENCE_ID_RE.findall(match.group(1).upper())
                return " ".join(f"[{cid}]" for cid in bind_ids(ids))

            text = _CITATION_RE.sub(replace, text)
            return re.sub(
                r"(?:\[E\d+\]\s*){2,}",
                lambda match: " ".join(f"[{cid}]" for cid in _citations_in_text(match[0])) + " ",
                text,
            ).rstrip()

        dropped: list[str] = list(report.dropped_citations)
        conclusions: list[ReportClaim] = []
        seen_statements: dict[str, int] = {}
        for claim in report.conclusions:
            kept = bind_ids(claim.evidence_ids)
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
            conclusions.append(
                claim.model_copy(
                    update={
                        "statement": bind_text(claim.statement),
                        "evidence_ids": kept,
                        "confidence": confidence,
                    }
                )
            )
        sections: list[ReportSection] = []
        for section in report.sections:
            kept = bind_ids(section.evidence_ids)
            dropped.extend(cid for cid in section.evidence_ids if cid not in allowed)
            body = bind_text(section.body)
            for cited in set(section.evidence_ids) - set(kept):
                body = body.replace(f"[{cited}]", "")
            sections.append(section.model_copy(update={"evidence_ids": kept, "body": body}))
        bound = report.model_copy(
            update={
                "conclusions": conclusions,
                "sections": sections,
                "executive_summary": bind_text(report.executive_summary),
                "recommendations": [bind_text(text) for text in report.recommendations],
                "limitations": [bind_text(text) for text in report.limitations],
                "dropped_citations": sorted(set(dropped)),
            }
        )
        return self._add_diagnostics(bound, verification, injection_ids) if include_diagnostics else bound

    def _add_diagnostics(
        self, report: FinalReport, verification: VerificationReport, injection_ids: set[str]
    ) -> FinalReport:
        """Add runtime diagnostics after model prose is reviewed."""
        limitations = list(report.limitations)
        if injection_ids:
            limitations.append(
                "以下证据包含提示注入（prompt injection）指令特征，已从结论中排除，"
                "仅作为风险信号保留：" + ", ".join(sorted(injection_ids))
            )
        if not verification.sufficient and not any(
            "不足" in item or "缺口" in item or "insufficient" in item.lower() for item in limitations
        ):
            limitations.append("当前资料的质量或覆盖度不足，结论应视为待补检的初步判断。")
        if report.dropped_citations:
            limitations.append("已剔除 " + str(len(report.dropped_citations)) + " 条无法对应到证据的引用。")
        if verification.flagged_sources:
            limitations.append(
                "以下来源包含提示注入（prompt injection）特征或低质量信号，其内容仅按不可信数据引用，"
                "未执行其中的任何指令：" + ", ".join(verification.flagged_sources[:5])
            )
        return report.model_copy(update={"limitations": list(dict.fromkeys(limitations))})

    # -- fallback ----------------------------------------------------------- #
    def _fallback_report(
        self, plan: ResearchPlan, usable: list[Evidence], verification: VerificationReport
    ) -> FinalReport:
        if not usable:
            return FinalReport(
                title=f"研究报告：{truncate(plan.objective, 60)}",
                executive_summary=(
                    f"仅依据所选资料，无法确认：{plan.objective}。"
                    "资料不足不等于否定结论，需补充能回答该问题的资料。"
                ),
                conclusions=[],
                sections=[
                    ReportSection(
                        heading="证据缺口",
                        body="- 所有候选证据均未通过支撑性校验。",
                        evidence_ids=[],
                    )
                ],
                recommendations=["补充能回答该问题的资料后重新研究。"],
                limitations=["无可用证据，本报告不包含事实性结论。"],
            )
        conclusions = [
            ReportClaim(
                statement=e.claim,
                evidence_ids=[e.id],
                confidence=round(min(e.confidence + 0.1, 0.95), 3),
            )
            for e in usable
        ]
        conclusions = _deduplicate_claims(conclusions)
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
            executive_summary="\n".join(
                clean_statement(claim.statement) + " " + " ".join(f"[{cid}]" for cid in claim.evidence_ids)
                for claim in conclusions
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
    evidence = [e for e in evidence if e.id in evidence_ids]
    used_source_ids = [e.source_id for e in evidence]
    source_by_id = {s.id: s for s in sources}
    reference_index: dict[str, int] = {}
    documents: dict[str, int] = {}
    for source_id in used_source_ids:
        if source_id not in reference_index:
            source = source_by_id.get(source_id)
            key = f"doc:{source.doc_id}" if source and source.doc_id else f"source:{source_id}"
            documents.setdefault(key, len(documents) + 1)
            reference_index[source_id] = documents[key]

    lines: list[str] = [f"# {report.title}", ""]
    lines += ["## 摘要", "", report.executive_summary, ""]
    if report.conclusions:
        lines += ["## 有依据的结论", ""]
        for claim in report.conclusions:
            refs = " ".join(f"[{cid}]" for cid in claim.evidence_ids)
            suffix = f" {refs}" if refs else " *（未绑定引用）*"
            label = {"supported": "有依据", "inference": "推断，待核实", "insufficient": "证据不足"}[
                claim.assessment
            ]
            lines.append(f"- **{label}**：{clean_statement(claim.statement)}{suffix}")
        lines.append("")
    if report.sections:
        lines += ["## 分析", ""]
        for section in report.sections:
            lines += [f"### {section.heading}", "", section.body, ""]
    if report.recommendations:
        lines += ["## 建议（推断，需结合实际核实）", ""]
        lines += [f"- {item}" for item in report.recommendations]
        lines.append("")
    if report.limitations:
        lines += ["## 证据不足与待核实", ""]
        lines += [f"- {item}" for item in report.limitations]
        lines.append("")
    if reference_index:
        lines += ["## 参考文献 (References)", ""]
        rendered: set[int] = set()
        for source_id, index in sorted(reference_index.items(), key=lambda kv: kv[1]):
            if index in rendered:
                continue
            rendered.add(index)
            source = source_by_id.get(source_id)
            if source is None:
                lines.append(f"{index}. `{source_id}` (source metadata unavailable)")
                continue
            location = source.metadata.get("filename") or source.url or source.locator or source.doc_id
            lines.append(f"{index}. **{source.title}** — `{location}`")
        lines.append("")
    if evidence:
        lines += ["## 引用原文（核对用）", ""]
        lines += ["| 引用 | 原文 | 来源 |", "| --- | --- | --- |"]
        quotes: dict[tuple[int | None, str, str], tuple[Evidence, list[str]]] = {}
        for item in evidence:
            ref = reference_index.get(item.source_id)
            quote_key = (ref, "" if ref else item.source_id, _normalise_statement(item.quote))
            if quote_key not in quotes:
                quotes[quote_key] = (item, [])
            quotes[quote_key][1].append(item.id)
        for item, ids in quotes.values():
            source = source_by_id.get(item.source_id)
            ref = reference_index.get(item.source_id)
            label = f"[{ref}] {source.title}" if source and ref else item.source_id
            lines.append(f"| {', '.join(ids)} | {_cell(item.quote, limit=4000)} | {_cell(label)} |")
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

    return normalize_text(unicodedata.normalize("NFKC", clean_statement(text))).rstrip(".。 ")


def _deduplicate_claims(claims: list[ReportClaim]) -> list[ReportClaim]:
    unique: dict[str, ReportClaim] = {}
    for claim in claims:
        key = _normalise_statement(claim.statement)
        previous = unique.get(key)
        if previous:
            claim = previous.model_copy(
                update={
                    "evidence_ids": list(dict.fromkeys(previous.evidence_ids + claim.evidence_ids)),
                    "confidence": max(previous.confidence, claim.confidence),
                }
            )
        unique[key] = claim
    return list(unique.values())
