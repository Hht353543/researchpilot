"""Run the golden-dataset benchmark and regenerate docs/evaluation.md + docs/resume.md.

Usage:
    python scripts/run_benchmark.py --provider mock          # offline, no API key
    python scripts/run_benchmark.py --provider openai        # real model (needs API_KEY)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchpilot.evaluation.report import write_docs
from researchpilot.evaluation.runner import EvaluationRunner


def main() -> int:
    parser = argparse.ArgumentParser(description="ResearchPilot benchmark runner")
    parser.add_argument("--provider", choices=["mock", "openai"], default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--categories", nargs="*", default=None)
    parser.add_argument("--task-ids", nargs="*", default=None)
    parser.add_argument("--no-docs", action="store_true")
    args = parser.parse_args()

    runner = EvaluationRunner(provider_name=args.provider)
    report = runner.run(categories=args.categories, task_ids=args.task_ids, limit=args.limit)

    metrics = report.metrics
    print("=" * 78)
    print(f"ResearchPilot evaluation · provider={report.provider} · model={report.model}")
    print(f"mode: {report.mode}")
    print("=" * 78)
    print(f"tasks={metrics.tasks} passed={metrics.passed} success={metrics.task_success_rate:.1%}")
    print(
        f"retrieval_recall={metrics.retrieval_recall:.1%} "
        f"context_relevance={metrics.context_relevance:.1%} "
        f"citation_correctness={metrics.citation_correctness:.1%}"
    )
    print(
        f"tool_selection_accuracy={metrics.tool_selection_accuracy:.1%} "
        f"tool_selection_f1={metrics.tool_selection_f1:.1%} "
        f"tool_success_rate="
        f"{'n/a' if metrics.tool_success_rate is None else f'{metrics.tool_success_rate:.1%}'}"
    )
    print(
        f"avg_latency={metrics.avg_latency_s:.2f}s p95={metrics.p95_latency_s:.2f}s "
        f"tokens={metrics.total_tokens} cost=${metrics.total_cost_usd:.6f}"
    )
    for row in report.categories:

        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.0%}"

        print(
            f"  {row.category:<20} {row.passed}/{row.tasks} ({row.task_success_rate:.0%}) "
            f"latency={row.avg_latency_s:.2f}s tokens={row.avg_tokens:.0f} "
            f"recall={pct(row.retrieval_recall)} citation={pct(row.citation_correctness)} "
            f"tool_f1={pct(row.tool_selection_f1)}"
        )
    if report.failed_checks:
        print("failed checks:", report.failed_checks)
    if not args.no_docs:
        for path in write_docs(report):
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
