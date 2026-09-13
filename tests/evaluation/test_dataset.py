"""Golden dataset integrity: size, coverage, ids, fault setups."""

from __future__ import annotations

from pathlib import Path

from researchpilot.evaluation.dataset import dataset_stats, load_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CATEGORIES = {
    "simple_fact",
    "multi_step",
    "rag",
    "tool_calling",
    "mcp",
    "multi_agent",
    "citation",
    "prompt_injection",
    "tool_abuse",
    "timeout_recovery",
    "no_result",
    "bad_source",
}


def test_golden_dataset_has_at_least_30_tasks() -> None:
    tasks = load_dataset(REPO_ROOT / "eval" / "golden_dataset.jsonl")
    assert len(tasks) >= 30
    assert len({task.id for task in tasks}) == len(tasks)


def test_golden_dataset_covers_all_required_categories() -> None:
    tasks = load_dataset(REPO_ROOT / "eval" / "golden_dataset.jsonl")
    stats = dataset_stats(tasks)
    assert set(stats["categories"]) >= EXPECTED_CATEGORIES
    for category in EXPECTED_CATEGORIES:
        assert stats["categories"][category] >= 2, f"{category} needs at least 2 tasks"


def test_golden_dataset_expectations_are_checkable() -> None:
    tasks = load_dataset(REPO_ROOT / "eval" / "golden_dataset.jsonl")
    for task in tasks:
        assert task.question.strip()
        expectations = task.expectations
        has_check = any(
            [
                task.expected_docs,
                task.expected_tools,
                task.setup,
                expectations.required_any,
                expectations.forbidden,
                expectations.require_gap_statement,
                expectations.status_in,
                expectations.min_citation_integrity,
                expectations.min_retrieval_recall,
                expectations.min_tool_failures,
                expectations.max_tool_failures is not None,
                expectations.max_latency_s is not None,
            ]
        )
        assert has_check, f"{task.id} has no machine-checkable expectation"
        for group in task.expectations.required_any:
            assert group, f"{task.id} has an empty required_any group"


def test_fault_injection_setups_reference_real_files() -> None:
    tasks = load_dataset(REPO_ROOT / "eval" / "golden_dataset.jsonl")
    for task in tasks:
        fault = task.setup.get("fault")
        if fault:
            assert fault["mode"] in {"timeout", "failure", "empty"}
            assert fault["tool"] in {
                "knowledge_search",
                "web_search",
                "document_reader",
                "calculator",
                "metadata",
            }
        for item in task.setup.get("ingest", []) or []:
            assert (REPO_ROOT / item["path"]).exists(), item["path"]


def test_expected_docs_exist_in_default_knowledge_base() -> None:
    from researchpilot.rag.loader import DocumentLoader

    documents = DocumentLoader().load_path(REPO_ROOT / "data" / "knowledge_base")
    known = {doc.doc_id for doc in documents}
    web_ids = {f"web_{index:03d}" for index in range(20)}
    sample_ids = {"doc_poisoned_sample", "doc_marketing_whitepaper"}
    tasks = load_dataset(REPO_ROOT / "eval" / "golden_dataset.jsonl")
    for task in tasks:
        for doc_id in task.expected_docs:
            assert doc_id in known | web_ids | sample_ids, f"{task.id}: unknown doc {doc_id}"
