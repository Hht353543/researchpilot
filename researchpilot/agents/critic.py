"""CriticAgent: hunts for missing coverage, weak logic and stale evidence."""

from __future__ import annotations

from researchpilot.agents.base import BaseAgent
from researchpilot.llm.prompts import CRITIC_SYSTEM, critic_user
from researchpilot.schemas import (
    CritiqueIssue,
    CritiqueReport,
    EvidenceBundle,
    ResearchPlan,
    VerificationReport,
)
from researchpilot.utils import overlap_ratio, truncate


class CriticAgent(BaseAgent):
    name = "CriticAgent"

    def run(
        self,
        plan: ResearchPlan,
        bundle: EvidenceBundle,
        verification: VerificationReport,
    ) -> CritiqueReport:
        runtime = self.runtime
        subtask_map = {s.id: s.question for s in plan.subtasks if s.intent != "synthesis"}
        evidence_dump = [e.model_dump() for e in bundle.evidence]
        with self.span(input={"evidence": len(bundle.evidence)}) as span:
            report: CritiqueReport | None = None
            try:
                result = runtime.llm.run(
                    CritiqueReport,
                    agent=self.name,
                    system=CRITIC_SYSTEM,
                    user=critic_user(
                        plan.objective,
                        plan.model_dump(),
                        evidence_dump,
                        verification.model_dump(),
                        self._safe_topics(),
                    ),
                    hints={
                        "objective": plan.objective,
                        "plan": plan.model_dump(),
                        "evidence": evidence_dump,
                        "verification": verification.model_dump(),
                        "subtask_map": subtask_map,
                        "kb_topics": self._safe_topics(),
                    },
                    purpose="critic",
                    max_tokens=1500,
                )
                report = result.value  # type: ignore[assignment]
            except Exception as exc:
                runtime.errors.append(f"critic: {type(exc).__name__}: {exc}")
            report = self._augment(report, subtask_map, bundle, verification)
            runtime.tracer.end_span(
                span,
                output={
                    "issues": [i.model_dump() for i in report.issues],
                    "needs_more_research": report.needs_more_research,
                    "follow_up_queries": report.follow_up_queries,
                    "coverage": report.coverage,
                },
            )
        return report

    def _safe_topics(self) -> list[str]:
        try:
            return list(self.runtime.knowledge_base.topics())
        except Exception as exc:
            self.runtime.errors.append(f"critic: {type(exc).__name__}: {exc}")
            return []

    def _augment(
        self,
        report: CritiqueReport | None,
        subtask_map: dict[str, str],
        bundle: EvidenceBundle,
        verification: VerificationReport,
    ) -> CritiqueReport:
        """Deterministic safety net: gaps found in code are never silently dropped."""
        issues: list[CritiqueIssue] = list(report.issues) if report else []
        covered = {e.subtask_id for e in bundle.evidence}
        uncovered = [q for sid, q in subtask_map.items() if sid not in covered]
        follow_ups = list(report.follow_up_queries) if report else []
        for question in uncovered:
            if not any(overlap_ratio(question, q) > 0.5 for q in follow_ups):
                follow_ups.append(truncate(question, 160))
            if not any(
                i.category == "coverage" and overlap_ratio(question, i.description) > 0.5 for i in issues
            ):
                issues.append(
                    CritiqueIssue(
                        severity="high",
                        category="coverage",
                        description=f"子任务缺少证据：{truncate(question, 90)}",
                        suggested_fix="对该子问题补检一轮（知识库 + Web）",
                    )
                )
        if verification.unsupported_claims and not any(i.category == "citation" for i in issues):
            issues.append(
                CritiqueIssue(
                    severity="medium",
                    category="citation",
                    description=f"{len(verification.unsupported_claims)} 条结论证据不足",
                    suggested_fix="删除或降级这些结论",
                )
            )
        needs_more = (
            bool(uncovered) or not verification.sufficient or bool(report and report.needs_more_research)
        )
        coverage = {
            "subtask_coverage": round(len(covered & set(subtask_map)) / max(len(subtask_map), 1), 3),
            "evidence_count": float(len(bundle.evidence)),
            "high_severity_issues": float(sum(1 for i in issues if i.severity == "high")),
        }
        return CritiqueReport(
            issues=issues,
            needs_more_research=needs_more,
            follow_up_queries=follow_ups[:5],
            coverage=coverage,
            overall_assessment=(
                report.overall_assessment
                if report and report.overall_assessment
                else ("仍需补检" if needs_more else "覆盖度与引用强度达标")
            ),
        )
