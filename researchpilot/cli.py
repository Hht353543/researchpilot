"""Command line interface: research / serve / ingest / mcp / bench."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchpilot", description="ResearchPilot CLI")
    parser.add_argument("--version", action="version", version="researchpilot 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    research = sub.add_parser("research", help="run one research task")
    research.add_argument("question")
    research.add_argument("--provider", choices=["mock", "openai"], default=None)
    research.add_argument("--model", default=None)
    research.add_argument("--temperature", type=float, default=None)
    research.add_argument("--presence-penalty", type=float, default=None)
    research.add_argument("--frequency-penalty", type=float, default=None)
    research.add_argument("--top-k", type=int, default=None)
    research.add_argument("--max-iterations", type=int, default=None)
    research.add_argument("--json-out", default=None, help="write the full result as JSON")
    research.add_argument("--markdown-out", default=None, help="write the report markdown")
    research.add_argument("--quiet", action="store_true")

    serve = sub.add_parser("serve", help="run the FastAPI app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    ingest = sub.add_parser("ingest", help="ingest documents into the knowledge base")
    ingest.add_argument("--path", default=None, help="file or directory (default: configured KB path)")
    ingest.add_argument("--text", default=None, help="inline text to ingest")
    ingest.add_argument("--title", default="inline document")

    mcp = sub.add_parser("mcp", help="run the MCP server")
    mcp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    mcp.add_argument("--host", default=None)
    mcp.add_argument("--port", type=int, default=None)

    bench = sub.add_parser("bench", help="run the golden-dataset benchmark")
    bench.add_argument("--provider", choices=["mock", "openai"], default=None)
    bench.add_argument("--limit", type=int, default=None)
    bench.add_argument("--categories", nargs="*", default=None)
    bench.add_argument("--no-docs", action="store_true", help="skip regenerating docs/")
    return parser


def _run_research(args: argparse.Namespace) -> int:
    from researchpilot.config import get_settings
    from researchpilot.llm.factory import build_provider
    from researchpilot.pipeline import ResearchPipeline
    from researchpilot.schemas import ResearchRequest, ResearchSettings

    settings = get_settings()
    provider = build_provider(settings, args.provider) if args.provider else None
    pipeline = ResearchPipeline(settings=settings, provider=provider)
    request = ResearchRequest(
        question=args.question,
        settings=ResearchSettings(
            model=args.model,
            temperature=args.temperature,
            presence_penalty=args.presence_penalty,
            frequency_penalty=args.frequency_penalty,
            top_k=args.top_k,
            max_iterations=args.max_iterations,
        ),
    )
    result = pipeline.run(request)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.markdown_out and result.report:
        Path(args.markdown_out).write_text(result.report.markdown, encoding="utf-8")
    if not args.quiet:
        print(result.report.markdown if result.report else "(no report)")
        print(
            f"\n---\nstatus={result.status} latency={result.metrics.latency_ms / 1000:.2f}s "
            f"tokens={result.metrics.usage.total_tokens} tool_calls={result.metrics.tool_calls} "
            f"evidence={len(result.evidence.evidence)} sources={len(result.evidence.sources)}",
            file=sys.stderr,
        )
        if result.errors:
            print("errors:\n  " + "\n  ".join(result.errors), file=sys.stderr)
    return 0 if result.status in {"succeeded", "degraded"} else 1


def _run_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from researchpilot.api.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, reload=args.reload)
    return 0


def _run_ingest(args: argparse.Namespace) -> int:
    from researchpilot.rag.knowledge_base import KnowledgeBase

    kb = KnowledgeBase.load_or_create()
    if args.text:
        report = kb.ingest_text(args.text, title=args.title, source="cli://inline")
    else:
        report = kb.ingest_path(args.path or kb.settings.kb_path)
    kb.save()
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    print(f"knowledge base: {kb.stats()}")
    return 0


def _run_mcp(args: argparse.Namespace) -> int:
    from researchpilot.config import get_settings
    from researchpilot.mcp.server import create_http_app, serve_stdio

    settings = get_settings()
    if args.transport == "http":
        import uvicorn

        uvicorn.run(
            create_http_app(),
            host=args.host or settings.mcp_host,
            port=args.port or settings.mcp_port,
        )
    else:
        serve_stdio()
    return 0


def _run_bench(args: argparse.Namespace) -> int:
    from researchpilot.evaluation.report import write_docs
    from researchpilot.evaluation.runner import EvaluationRunner

    runner = EvaluationRunner(provider_name=args.provider)
    report = runner.run(categories=args.categories, limit=args.limit)
    metrics = report.metrics
    print(
        f"tasks={metrics.tasks} passed={metrics.passed} "
        f"success={metrics.task_success_rate:.1%} recall={metrics.retrieval_recall:.1%} "
        f"citation={metrics.citation_correctness:.1%} tool_f1={metrics.tool_selection_f1:.1%} "
        f"tool_success={metrics.tool_success_rate:.1%} avg_latency={metrics.avg_latency_s:.2f}s "
        f"tokens={metrics.total_tokens} cost=${metrics.total_cost_usd:.6f}"
    )
    for row in report.categories:
        print(
            f"  {row.category:<20} {row.passed}/{row.tasks} "
            f"({row.task_success_rate:.0%}) latency={row.avg_latency_s:.2f}s"
        )
    if not args.no_docs:
        for path in write_docs(report):
            print(f"wrote {path}")
    return 0 if metrics.task_success_rate >= 0.8 else 1


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = _build_parser().parse_args(argv)
    handlers = {
        "research": _run_research,
        "serve": _run_serve,
        "ingest": _run_ingest,
        "mcp": _run_mcp,
        "bench": _run_bench,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
