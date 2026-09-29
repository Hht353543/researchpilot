"""Retrieval ablation: embedder x strategy on the golden dataset's expected docs.

Measures Recall@k / Precision@k (context relevance) / MRR for the retrieval module
alone - no LLM involved - so the numbers isolate retrieval quality from generation.

    python scripts/retrieval_ablation.py                       # hash embedder
    RESEARCHPILOT_EMBEDDING_PROVIDER=openai \
    RESEARCHPILOT_EMBEDDING_MODEL=shibing624/text2vec-base-chinese \
    RESEARCHPILOT_EMBEDDING_DIM=768 \
    BASE_URL=http://127.0.0.1:8774/v1 API_KEY=local \
      python scripts/retrieval_ablation.py --label "text2vec-base-chinese"
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchpilot.config import Settings
from researchpilot.evaluation.dataset import load_dataset
from researchpilot.rag.knowledge_base import KnowledgeBase

STRATEGIES = ("dense", "keyword", "hybrid")


def evaluate(
    kb: KnowledgeBase, tasks: list, *, strategies: tuple[str, ...], top_k: int
) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {}
    for strategy in strategies:
        recalls: list[float] = []
        precisions: list[float] = []
        reciprocal_ranks: list[float] = []
        for task in tasks:
            if not task.expected_docs:
                continue
            retrieval = kb.search(task.question, top_k=top_k, strategy=strategy, rewrite=True)
            doc_ids = [hit.chunk.doc_id for hit in retrieval.hits]
            expected = set(task.expected_docs)
            hits = [doc for doc in doc_ids if doc in expected]
            recalls.append(len(set(hits)) / len(expected))
            precisions.append(len(hits) / max(len(doc_ids), 1))
            rank = next((i for i, doc in enumerate(doc_ids, start=1) if doc in expected), 0)
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        results[strategy] = {
            "tasks": float(len(recalls)),
            "recall": round(statistics.fmean(recalls), 4) if recalls else 0.0,
            "precision": round(statistics.fmean(precisions), 4) if precisions else 0.0,
            "mrr": round(statistics.fmean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval ablation over the golden dataset")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--label", default=None, help="embedder label for the report heading")
    parser.add_argument("--out", default=None, help="append a Markdown table to this file")
    parser.add_argument("--dataset", default="eval/golden_dataset.jsonl")
    args = parser.parse_args()

    settings = Settings()
    kb = KnowledgeBase.load_or_create(settings)
    tasks = load_dataset(args.dataset)
    label = args.label or f"{settings.embedding_provider}({getattr(kb.embedder, 'dimension', '?')}d)"
    stats = kb.stats()
    results = evaluate(kb, tasks, strategies=STRATEGIES, top_k=args.top_k)

    heading = (
        f"### Embedder: `{label}` · index: {stats['documents']} docs / "
        f"{stats['chunks']} chunks · k={args.top_k}"
    )
    lines = [
        heading,
        "",
        "| strategy | tasks with expectations | Recall@k | Precision@k (context relevance) | MRR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for strategy, row in results.items():
        lines.append(
            f"| {strategy} | {int(row['tasks'])} | {row['recall']:.1%} | "
            f"{row['precision']:.1%} | {row['mrr']:.3f} |"
        )
    lines.append("")
    table = "\n".join(lines)
    print(table)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(table + "\n")
        print(f"appended to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
