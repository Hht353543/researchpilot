"""Evaluation runner: executes the golden dataset and records real measurements."""

from __future__ import annotations

import copy
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.evaluation.dataset import GoldenTask, dataset_stats, load_dataset
from researchpilot.evaluation.fault_injection import build_fault_tool
from researchpilot.evaluation.judge import TaskJudgement, judge_task
from researchpilot.evaluation.metrics import (
    CategoryMetrics,
    EvaluationMetrics,
    aggregate,
    by_category,
    failed_checks,
)
from researchpilot.llm.base import LLMProvider
from researchpilot.llm.factory import build_provider
from researchpilot.mcp.client import InProcessMcpClient
from researchpilot.mcp.server import McpServer
from researchpilot.observability.trace import TraceStore
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.rag.loader import DocumentLoader
from researchpilot.rag.vector_store import VectorStore
from researchpilot.schemas import ResearchRequest
from researchpilot.store import ResultStore
from researchpilot.tools.registry import ToolPolicy
from researchpilot.utils import new_id


class EvaluationReport(BaseModel):
    run_id: str
    generated_at: str
    provider: str
    model: str
    mode: str
    dataset_path: str
    dataset: dict[str, Any] = Field(default_factory=dict)
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)
    categories: list[CategoryMetrics] = Field(default_factory=list)
    judgements: list[TaskJudgement] = Field(default_factory=list)
    failed_checks: dict[str, int] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    duration_s: float = 0.0
    settings_snapshot: dict[str, Any] = Field(default_factory=dict)


class EvaluationRunner:
    """Runs the golden dataset through the real pipeline (no stubs above the tool layer)."""

    def __init__(
        self,
        *,
        dataset_path: str | Path | None = None,
        settings: Settings | None = None,
        provider_name: str | None = None,
        provider: LLMProvider | None = None,
        output_dir: str | Path = Path("benchmarks"),
        knowledge_base: KnowledgeBase | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.dataset_path = Path(dataset_path) if dataset_path else None
        self.provider_name = provider_name or self.settings.provider
        self.provider = provider
        self.output_dir = Path(output_dir)
        self.knowledge_base = knowledge_base or KnowledgeBase.load_or_create(self.settings)

    # -- execution --------------------------------------------------------- #
    def run(
        self,
        *,
        tasks: list[GoldenTask] | None = None,
        categories: list[str] | None = None,
        task_ids: list[str] | None = None,
        limit: int | None = None,
        persist: bool = True,
    ) -> EvaluationReport:
        dataset = tasks or load_dataset(self.dataset_path)
        if categories:
            wanted = set(categories)
            dataset = [t for t in dataset if t.category in wanted]
        if task_ids:
            wanted_ids = set(task_ids)
            dataset = [t for t in dataset if t.id in wanted_ids]
        if limit:
            dataset = dataset[:limit]

        owns_provider = self.provider is None
        provider = self.provider or build_provider(self.settings, self.provider_name)
        try:
            return self._run_dataset(dataset, provider, persist=persist)
        finally:
            if owns_provider:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()

    def _run_dataset(
        self,
        dataset: list[GoldenTask],
        provider: LLMProvider,
        *,
        persist: bool,
    ) -> EvaluationReport:
        started = time.perf_counter()
        judgements: list[TaskJudgement] = []
        for task in dataset:
            judgements.append(self._run_task(task, provider))
        duration = time.perf_counter() - started

        metrics = aggregate(judgements)
        report = EvaluationReport(
            run_id=new_id("eval"),
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            provider=self.provider_name,
            model=getattr(provider, "model_name", lambda: "unknown")(),
            mode="offline (deterministic mock provider)" if self.provider_name == "mock" else "live model",
            dataset_path=str(self.dataset_path or Path("eval/golden_dataset.jsonl")),
            dataset=dataset_stats(load_dataset(self.dataset_path)),
            metrics=metrics,
            categories=by_category(judgements),
            judgements=judgements,
            failed_checks=failed_checks(judgements),
            environment={
                "python": platform.python_version(),
                "platform": platform.platform(),
                "embedding_provider": self.settings.embedding_provider,
                "embedding_dim": self.settings.embedding_dim,
                "retrieval_top_k": self.settings.top_k,
                "max_iterations": self.settings.max_iterations,
                "web_search_mode": self.settings.web_search_mode,
                "mcp_transport": self.settings.mcp_transport,
            },
            duration_s=round(duration, 3),
            settings_snapshot={
                "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_tokens,
                "token_budget": self.settings.token_budget,
                "rerank_strategy": self.settings.rerank_strategy,
            },
        )
        if persist:
            self._persist(report)
        return report

    def _run_task(self, task: GoldenTask, provider: LLMProvider) -> TaskJudgement:
        knowledge_base = self.knowledge_base
        ingest = task.setup.get("ingest") or []
        if ingest:
            knowledge_base = self._copy_knowledge_base()
            for item in ingest:
                path = item.get("path")
                if not path:
                    continue
                documents = DocumentLoader().load_path(path)
                knowledge_base.ingest_documents(documents)

        mcp_client = InProcessMcpClient(McpServer(self.settings, knowledge_base=knowledge_base))
        fault = task.setup.get("fault")
        tool_overrides = (
            (lambda registry, spec=fault: {str(spec.get("tool")): build_fault_tool(registry, spec)})
            if fault
            else None
        )
        trace_store = TraceStore(persist=False)
        max_calls = task.setup.get("max_tool_calls")
        policy = ToolPolicy(max_calls_per_task=int(max_calls)) if max_calls else None
        pipeline = ResearchPipeline(
            settings=self.settings,
            provider=provider,
            knowledge_base=knowledge_base,
            trace_store=trace_store,
            result_store=ResultStore(persist=False),
            mcp_client=mcp_client,
            tool_overrides=tool_overrides,
            tool_policy=policy,
        )
        request = ResearchRequest(
            question=task.question,
            settings=self._request_settings(task),
            mode="sync",
        )
        started = time.perf_counter()
        result = pipeline.run(request)
        latency = time.perf_counter() - started
        trace = trace_store.get(result.task_id)
        judgement = judge_task(
            task,
            result,
            trace,
            latency_s=latency,
            live_model=self.provider_name != "mock",
        )
        judgement.notes = task.notes or judgement.notes
        return judgement

    def _request_settings(self, task: GoldenTask) -> Any:
        from researchpilot.schemas import ResearchSettings

        overrides = task.setup.get("settings") or {}
        data = {
            "top_k": overrides.get("top_k", self.settings.top_k),
            "max_iterations": overrides.get("max_iterations", self.settings.max_iterations),
            "temperature": overrides.get("temperature", self.settings.temperature),
        }
        return ResearchSettings(**data)

    def _copy_knowledge_base(self) -> KnowledgeBase:
        store: VectorStore = copy.deepcopy(self.knowledge_base.store)
        return KnowledgeBase(
            self.settings,
            store=store,
            embedder=self.knowledge_base.embedder,
        )

    def _persist(self, report: EvaluationReport) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = report.generated_at.replace(":", "").replace("-", "")
        payload = report.model_dump(mode="json")
        target = self.output_dir / f"eval_{report.provider}_{stamp}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest = self.output_dir / f"latest_{report.provider}.json"
        latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target


def load_report(path: str | Path) -> EvaluationReport:
    return EvaluationReport.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
