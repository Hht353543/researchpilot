"""Verify the project against a *real* model provider with one command.

Usage:
    export API_KEY=sk-...            # or RESEARCHPILOT_API_KEY
    export MODEL=gpt-4o-mini
    export BASE_URL=https://api.openai.com/v1

    python scripts/verify_live_model.py --probe-only
    python scripts/verify_live_model.py --task-ids fact-01-mcp-protocol rag-02-hybrid-rerank
    python scripts/verify_live_model.py --limit 35      # full golden dataset, real numbers

The script never prints the key. It fails fast with an explicit message when the
credential is missing or rejected, so the "real model" gap can be closed in one
step by whoever owns a valid key.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import ChatMessage, LLMConfigError, LLMError
from researchpilot.llm.openai_provider import OpenAICompatibleProvider
from researchpilot.schemas import CritiqueReport


def _settings() -> Settings:
    base = get_settings()
    return base.model_copy(
        update={
            "provider": "openai",
            "api_key": os.environ.get("RESEARCHPILOT_API_KEY") or os.environ.get("API_KEY") or base.api_key,
            "model": os.environ.get("MODEL", base.model),
            "base_url": os.environ.get("BASE_URL", base.base_url),
        }
    )


def probe(settings: Settings) -> int:
    """One minimal structured-output call: cheapest possible real-model check."""
    if not (settings.api_key or "").strip():
        print(
            "ERROR: no credential found. Set API_KEY (or RESEARCHPILOT_API_KEY) before running.",
            file=sys.stderr,
        )
        return 2
    provider = OpenAICompatibleProvider(settings.model_copy(update={"max_tokens": 64, "max_retries": 1}))
    print(f"probe -> model={settings.model} base_url={settings.base_url}")
    try:
        response = provider.complete(
            [
                ChatMessage(role="system", content="You are a terse evaluator. Reply with JSON only."),
                ChatMessage(
                    role="user",
                    content=(
                        'Return {"issues": [], "needs_more_research": false, "follow_up_queries": [], '
                        '"coverage": {}, "overall_assessment": "probe ok"}'
                    ),
                ),
            ],
            response_schema=CritiqueReport,
            purpose="probe",
        )
    except LLMConfigError as exc:
        print(f"ERROR: credential/config rejected: {exc}", file=sys.stderr)
        return 3
    except LLMError as exc:
        print(f"ERROR: provider call failed: {exc}", file=sys.stderr)
        return 4
    parsed = CritiqueReport.model_validate_json(response.text)
    print(
        f"probe -> ok: finish={response.finish_reason} attempts={response.attempts} "
        f"tokens={response.usage.total_tokens} payload={parsed.overall_assessment!r}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ResearchPilot against a real model provider")
    parser.add_argument("--probe-only", action="store_true", help="only run the 1-call probe")
    parser.add_argument("--limit", type=int, default=None, help="number of golden-dataset tasks")
    parser.add_argument("--task-ids", nargs="*", default=None, help="specific golden-dataset task ids")
    parser.add_argument("--no-docs", action="store_true", help="do not regenerate docs/")
    args = parser.parse_args()

    settings = _settings()
    code = probe(settings)
    if code != 0 or args.probe_only:
        return code

    from researchpilot.evaluation.report import write_docs
    from researchpilot.evaluation.runner import EvaluationRunner

    runner = EvaluationRunner(settings=settings, provider_name="openai")
    report = runner.run(task_ids=args.task_ids, limit=args.limit)
    metrics = report.metrics
    print(
        f"live eval -> tasks={metrics.tasks} passed={metrics.passed} "
        f"success={metrics.task_success_rate:.1%} recall={metrics.retrieval_recall:.1%} "
        f"citation={metrics.citation_correctness:.1%} tool_f1={metrics.tool_selection_f1:.1%} "
        f"latency={metrics.avg_latency_s:.2f}s tokens={metrics.total_tokens} "
        f"cost=${metrics.total_cost_usd:.4f}"
    )
    if not args.no_docs:
        for path in write_docs(report):
            print(f"wrote {path} (provider={report.provider})")
    print(
        "NOTE: docs/evaluation.md and docs/resume.md now describe a live-model run; "
        "re-run with --provider mock to restore the offline regression baseline."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
