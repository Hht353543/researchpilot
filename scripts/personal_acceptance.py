"""Run small personal-document experiments using a saved desktop connection.

Reuses the real provider and pipeline; stores reports for human review without
turning keyword checks or the model's own support verdicts into human scores.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-data-dir", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--followup", action="store_true")
    parser.add_argument("--max-total-tokens", type=int, default=180_000)
    parser.add_argument("--task-token-budget", type=int, default=30_000)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_root.resolve()))

    from researchpilot.desktop.settings import DesktopStore
    from researchpilot.llm.openai_provider import OpenAICompatibleProvider
    from researchpilot.pipeline import ResearchPipeline
    from researchpilot.rag.knowledge_base import KnowledgeBase
    from researchpilot.rag.loader import DocumentLoader
    from researchpilot.schemas import ResearchRequest
    from researchpilot.tools.registry import ToolPolicy

    store = DesktopStore(args.desktop_data_dir.resolve())
    store.load()
    if not store.api_key:
        parser.error("No saved desktop connection. Test and save the connection in the app first.")
    dataset_path = ROOT / "eval" / "personal_acceptance.json"
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))["cases"]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "experiment.json").exists():
        parser.error("Output already contains an experiment; use a new directory.")
    settings = store.runtime_settings(api_key=store.api_key).model_copy(
        update={
            "temperature": 0.1,
            "max_tokens": 1200,
            "max_iterations": 1,
            "top_k": 4,
            "token_budget_live": args.task_token_budget,
            "request_timeout_s": 120,
            "research_task_timeout_s": 600,
            "max_retries": 1,
            "kb_path": str(output / "empty-library"),
            "runs_path": str(output / "runs"),
        }
    )
    provider = OpenAICompatibleProvider(settings)
    responses: list[dict[str, Any]] = []

    def observe(response: Any) -> None:
        response.read()
        if response.is_success and response.request.url.path.endswith("/chat/completions"):
            data = response.json()
            responses.append(
                {
                    "model": data.get("model"),
                    "usage": data.get("usage", {}),
                    "received_at": datetime.now(UTC).isoformat(),
                }
            )

    provider._client.event_hooks["response"] = [observe]

    def balances() -> dict[str, Decimal]:
        if store.config.service != "deepseek":
            return {}
        response = provider._client.get("/user/balance")
        if not response.is_success:
            return {}
        return {
            item["currency"]: Decimal(item["total_balance"])
            for item in response.json().get("balance_infos", [])
        }

    def tokens() -> int:
        return sum(int(row["usage"].get("total_tokens", 0)) for row in responses)

    summary: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "code_root": str(args.code_root.resolve()),
        "model": settings.model,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(
            json.dumps(
                {
                    str(path.relative_to(args.code_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((args.code_root / "researchpilot").rglob("*.py"))
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "parameters": {
            key: getattr(settings, key)
            for key in (
                "temperature",
                "presence_penalty",
                "frequency_penalty",
                "max_tokens",
                "max_iterations",
                "top_k",
                "token_budget_live",
                "max_retries",
                "request_timeout_s",
                "research_task_timeout_s",
            )
        },
        "human_review": "pending",
        "allowed_tools": ["knowledge_search", "document_reader", "metadata", "calculator"],
        "cases": [],
        "responses": responses,
    }
    before = balances()
    try:
        for case in cases:
            if tokens() >= args.max_total_tokens:
                summary["stopped_reason"] = "total token limit reached"
                break
            case_dir = output / case["id"]
            case_dir.mkdir(exist_ok=True)
            case_settings = settings.model_copy(update={"runs_path": str(case_dir / "runs")})
            kb = KnowledgeBase(case_settings)
            documents = [
                doc
                for item in case["documents"]
                for doc in DocumentLoader().load_text(item["content"], filename=item["filename"])
            ]
            kb.ingest_documents(documents)
            pipeline = ResearchPipeline(
                settings=case_settings,
                provider=provider,
                knowledge_base=kb,
                tool_policy=ToolPolicy(
                    allowed_tools={"knowledge_search", "document_reader", "metadata", "calculator"}
                ),
            )
            requests = [("initial", case["question"])]
            if args.followup and case.get("followup"):
                requests.append(("followup", case["followup"]))
            if args.followup and case["id"] == "dates":
                requests.append(
                    ("followup", "2025 和 2026 年的收入如何变化？请给出增加金额及百分比，同时保留上线日期。")
                )
            parent = None
            try:
                for kind, question in requests:
                    if tokens() >= args.max_total_tokens:
                        summary["stopped_reason"] = "total token limit reached"
                        break
                    request = ResearchRequest(
                        question=question,
                        document_ids=[doc.doc_id for doc in documents],
                        parent_task_id=parent if kind == "followup" else None,
                    )
                    offset = len(responses)
                    started = time.perf_counter()
                    result = pipeline.run(request)
                    elapsed = time.perf_counter() - started
                    payload = result.model_dump(mode="json")
                    (case_dir / f"{kind}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    (case_dir / f"{kind}.md").write_text(
                        result.report.markdown if result.report else "No report.", encoding="utf-8"
                    )
                    summary["cases"].append(
                        {
                            "id": case["id"],
                            "kind": kind,
                            "task_id": result.task_id,
                            "status": result.status,
                            "quality": result.quality,
                            "latency_s": round(elapsed, 3),
                            "metrics": payload["metrics"],
                            "expected": case["expected"],
                            "question": question,
                            "errors": payload["errors"],
                            "document_ids": payload["document_ids"],
                            "parent_task_id": payload["parent_task_id"],
                            "http_calls": len(responses) - offset,
                            "http_tokens": sum(
                                int(row["usage"].get("total_tokens", 0)) for row in responses[offset:]
                            ),
                            "human_review": "pending",
                            "response_start": offset,
                            "response_end": len(responses),
                        }
                    )
                    parent = result.task_id
                    (output / "experiment.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(
                        f"{case['id']}/{kind}: {result.status}; "
                        f"calls={len(responses) - offset}; tokens={tokens()}",
                        flush=True,
                    )
            finally:
                pipeline.close()
                kb.close()
        after = balances()
        summary["account_balance_delta"] = {
            currency: str(amount - after[currency])
            for currency, amount in before.items()
            if currency in after
        }
        summary["billing_note"] = (
            "Balance delta includes any concurrent account activity; provider token usage is recorded "
            "separately. Pipeline price-table estimates are not invoices."
        )
    finally:
        summary["finished_at"] = datetime.now(UTC).isoformat()
        (output / "experiment.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
