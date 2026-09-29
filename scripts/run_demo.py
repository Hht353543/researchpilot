"""Run the flagship demo question and store the artifacts under examples/."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchpilot.config import get_settings
from researchpilot.observability.trace import TraceStore
from researchpilot.pipeline import ResearchPipeline
from researchpilot.schemas import ResearchRequest

DEMO_QUESTION = (
    "分析当前 AI Agent 在企业软件开发中的应用趋势，并给出技术路线、代表性项目、优缺点以及参考资料。"
)


def main() -> int:
    target = Path("examples")
    target.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    result = ResearchPipeline(settings=settings).run(ResearchRequest(question=DEMO_QUESTION))
    (target / "sample_report.md").write_text(
        result.report.markdown if result.report else "(no report)", encoding="utf-8"
    )
    (target / "sample_result.json").write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    trace = TraceStore(settings.runs_dir()).get(result.task_id)
    if trace is not None:
        (target / "sample_trace.json").write_text(
            json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"task_id={result.task_id} status={result.status}")
    print(
        f"evidence={len(result.evidence.evidence)} sources={len(result.evidence.sources)} "
        f"tools={result.metrics.tool_calls} tokens={result.metrics.usage.total_tokens} "
        f"latency={result.metrics.latency_ms / 1000:.2f}s"
    )
    print(f"artifacts written to {target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
