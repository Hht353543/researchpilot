"""VerifierAgent: checks that claims are actually supported by their sources."""

from __future__ import annotations

from typing import Any

from researchpilot.agents.base import BaseAgent
from researchpilot.llm.prompts import VERIFIER_SYSTEM, verifier_user
from researchpilot.schemas import (
    CitationCheck,
    EvidenceBundle,
    ResearchPlan,
    VerificationReport,
)
from researchpilot.security import detect_injection
from researchpilot.utils import content_overlap, overlap_ratio

_STATUS_VALUE = {"supported": 1.0, "weak": 0.5, "unsupported": 0.0}
_KIND_QUALITY = {
    "knowledge_base": 0.85,
    "document": 0.8,
    "mcp": 0.75,
    "web": 0.65,
    "computation": 0.9,
}


class VerifierAgent(BaseAgent):
    name = "VerifierAgent"

    def run(
        self,
        plan: ResearchPlan,
        bundle: EvidenceBundle,
        *,
        source_contents: dict[str, str] | None = None,
    ) -> VerificationReport:
        runtime = self.runtime
        subtask_map = {s.id: s.question for s in plan.subtasks if s.intent != "synthesis"}
        source_contents = source_contents or {
            sid: runtime.sources.content_of(sid) for sid in runtime.sources.as_dict()
        }
        evidence_dump = [e.model_dump() for e in bundle.evidence]
        sources_dump = runtime.sources.as_dict()

        with self.span(input={"evidence": len(bundle.evidence), "subtasks": len(subtask_map)}) as span:
            report: VerificationReport | None = None
            try:
                result = runtime.llm.run(
                    VerificationReport,
                    agent=self.name,
                    system=VERIFIER_SYSTEM,
                    user=verifier_user(plan.objective, evidence_dump, sources_dump),
                    hints={
                        "objective": plan.objective,
                        "evidence": evidence_dump,
                        "sources": sources_dump,
                        "subtask_map": subtask_map,
                    },
                    purpose="verifier",
                    max_tokens=2000,
                )
                report = result.value
            except Exception as exc:
                runtime.errors.append(f"verifier: {type(exc).__name__}: {exc}")
            report = self._audit(report, bundle, subtask_map, source_contents, plan.objective)
            notes = [f"{c.evidence_id}:{c.status}" for c in report.checks if c.status != "supported"]
            if notes:
                self.note("verification concerns: " + ", ".join(notes[:6]))
            runtime.tracer.end_span(
                span,
                output={
                    "checks": len(report.checks),
                    "unsupported": len(report.unsupported_claims),
                    "coverage": report.coverage_score,
                    "sufficient": report.sufficient,
                    "overall_score": report.overall_score,
                },
            )
        return report

    # -- deterministic audit ----------------------------------------------- #
    def _audit(
        self,
        report: VerificationReport | None,
        bundle: EvidenceBundle,
        subtask_map: dict[str, str],
        source_contents: dict[str, str],
        objective: str,
    ) -> VerificationReport:
        """Re-derive every check in code; LLM judgement alone is never trusted."""
        runtime = self.runtime
        checks: list[CitationCheck] = []
        unsupported: list[str] = []
        quality: dict[str, float] = {}
        flagged: list[str] = []
        source_meta = runtime.sources.as_dict()
        subtask_relevance: dict[str, float] = {}
        source_relevance: dict[str, float] = {}
        for source_id, meta in source_meta.items():
            metadata = meta.get("metadata") or {}
            haystack = " ".join(
                str(part)
                for part in (
                    meta.get("title", ""),
                    metadata.get("section", ""),
                    source_contents.get(source_id, "")[:2000],
                )
                if part
            )
            source_relevance[source_id] = content_overlap(objective, haystack)
        objective_relevance: dict[str, float] = {}
        for evidence in bundle.evidence:
            question = subtask_map.get(evidence.subtask_id, "")
            relevance = source_relevance.get(evidence.source_id, content_overlap(objective, evidence.quote))
            objective_relevance[evidence.id] = relevance
            if question:
                subtask_relevance[evidence.subtask_id] = max(
                    subtask_relevance.get(evidence.subtask_id, 0.0),
                    max(relevance, content_overlap(question, evidence.quote)),
                )
        subtopic_best = max(objective_relevance.values(), default=0.0)
        for subtask_id, relevance in list(subtask_relevance.items()):
            subtask_relevance[subtask_id] = max(relevance, subtopic_best * 0.6)
        for evidence in bundle.evidence:
            source_id = evidence.source_id
            content = source_contents.get(source_id) or runtime.sources.content_of(source_id)
            quality[source_id] = _source_quality(source_meta.get(source_id, {}))
            if not content:
                status = "unsupported"
                reason = "source id is not registered by any tool call"
            else:
                grounding = overlap_ratio(evidence.quote, content)
                support = overlap_ratio(evidence.claim, evidence.quote)
                relevant_subtask = subtask_map.get(evidence.subtask_id, "")
                relevance = objective_relevance.get(evidence.id, 0.0)
                subtask_best = subtask_relevance.get(evidence.subtask_id, relevance)
                recovery = min(grounding / 0.7, 1.0)
                combined = round(0.4 * support + 0.4 * recovery + 0.2 * min(relevance * 2, 1.0), 3)
                if relevant_subtask and subtask_best < 0.15:
                    status = "unsupported"
                    reason_prefix = "no retrieved source is topically related to the sub-task"
                elif grounding < 0.35:
                    # The quote must exist in the retrieved source. Claim/quote
                    # agreement alone can be faked by an ungrounded sentence, so
                    # grounding is the primary invariant, not the combined score.
                    status = "unsupported"
                    reason_prefix = "quote is not present in the cited source"
                elif grounding < 0.65:
                    status = "weak"
                    reason_prefix = "quote only partially matches the cited source"
                elif combined >= 0.6:
                    status = "supported"
                    reason_prefix = ""
                elif combined >= 0.35:
                    status = "weak"
                    reason_prefix = ""
                else:
                    status = "unsupported"
                    reason_prefix = ""
                reason = (
                    f"{reason_prefix}; quote grounding={grounding:.2f}, "
                    f"claim support={support:.2f}, relevance={relevance:.2f}"
                )
                if detect_injection(content):
                    reason += "; source contains prompt-injection patterns"
                    quality[source_id] = min(quality.get(source_id, 0.5), 0.4)
                    if source_id not in flagged:
                        flagged.append(source_id)
            if status == "unsupported":
                unsupported.append(evidence.claim)
            checks.append(
                CitationCheck(
                    evidence_id=evidence.id,
                    statement=evidence.claim,
                    status=status,  # type: ignore[arg-type]
                    reason=reason,
                    overlap=round(overlap_ratio(evidence.claim, evidence.quote), 3),
                )
            )
        supported_ids = {c.evidence_id for c in checks if c.status != "unsupported"}
        covered = {e.subtask_id for e in bundle.evidence if e.id in supported_ids}
        coverage = len([s for s in covered if s in subtask_map]) / max(len(subtask_map), 1)
        mean_status = sum(_STATUS_VALUE[c.status] for c in checks) / len(checks) if checks else 0.0
        mean_quality = sum(quality.values()) / max(len(quality), 1)
        unsupported_ratio = len(unsupported) / max(len(checks), 1)
        overall = round(
            0.4 * mean_status + 0.25 * coverage + 0.2 * (1 - unsupported_ratio) + 0.15 * mean_quality,
            3,
        )
        missing = [q for sid, q in subtask_map.items() if sid not in covered]
        sufficient = bool(checks) and coverage >= 0.5 and mean_status >= 0.6 and mean_quality >= 0.5
        return VerificationReport(
            checks=checks,
            unsupported_claims=unsupported,
            flagged_sources=flagged,
            source_quality={k: round(float(v), 3) for k, v in quality.items()},
            coverage_score=round(coverage, 3),
            sufficient=sufficient,
            missing_topics=missing,
            overall_score=overall,
        )


def _source_quality(meta: dict[str, Any]) -> float:
    kind = str(meta.get("kind", "web"))
    score = _KIND_QUALITY.get(kind, 0.6)
    metadata = meta.get("metadata") or {}
    if metadata.get("synthetic"):
        score = min(score, 0.5)
    declared = str(metadata.get("quality") or "").lower()
    if declared in {"low", "poor", "unverified", "marketing"}:
        score = min(score, 0.3)
    elif declared in {"high", "verified"}:
        score = min(score + 0.1, 0.98)
    if metadata.get("injection_signals"):
        score = min(score, 0.4)
    return round(score, 3)
