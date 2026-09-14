"""Spec §17: documentation must not reference tests or files that do not exist.

The traceability matrix and audit report cite many test names; this guard keeps
those claims honest as tests are renamed or removed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "audit_report.md",
    ROOT / "docs" / "requirements_traceability.md",
    ROOT / "docs" / "development_log.md",
    ROOT / "docs" / "evaluation_local_model.md",
    ROOT / "docs" / "retrieval_ablation.md",
]
TEST_NAME_RE = re.compile(r"\b(test_[a-z0-9_]{6,})\b")
TEST_PATH_RE = re.compile(r"\b(tests/[A-Za-z0-9_./-]+\.py)\b")


def _declared_test_names() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("*.py"):
        names.update(re.findall(r"def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8")))
    return names


def _test_module_stems() -> set[str]:
    return {path.stem for path in (ROOT / "tests").rglob("test_*.py")}


def test_documented_test_names_exist() -> None:
    declared = _declared_test_names()
    # Documents also cite file paths like tests/unit/test_planner.py; a bare
    # module name is not a test-function reference.
    stems = _test_module_stems()
    missing: dict[str, list[str]] = {}
    for doc in DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for name in set(TEST_NAME_RE.findall(text)):
            if name not in declared and name not in stems:
                missing.setdefault(str(doc.relative_to(ROOT)), []).append(name)
    assert not missing, f"documents cite non-existent tests: {missing}"


def test_documented_test_paths_exist() -> None:
    missing: dict[str, list[str]] = {}
    for doc in DOCS:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for rel in set(TEST_PATH_RE.findall(text)):
            if not (ROOT / rel).exists():
                missing.setdefault(str(doc.relative_to(ROOT)), []).append(rel)
    assert not missing, f"documents cite non-existent test files: {missing}"


def test_documented_commands_exist() -> None:
    """Commands quoted in the docs must reference real entry points."""
    scripts_and_modules = {
        "scripts/run_benchmark.py": ROOT / "scripts" / "run_benchmark.py",
        "scripts/run_demo.py": ROOT / "scripts" / "run_demo.py",
        "scripts/compose_smoke.py": ROOT / "scripts" / "compose_smoke.py",
        "scripts/retrieval_ablation.py": ROOT / "scripts" / "retrieval_ablation.py",
        "scripts/verify_live_model.py": ROOT / "scripts" / "verify_live_model.py",
        "scripts/local_model_server.py": ROOT / "scripts" / "local_model_server.py",
        "scripts/ci_dry_run.py": ROOT / "scripts" / "ci_dry_run.py",
        "scripts/verify_fresh_clone.py": ROOT / "scripts" / "verify_fresh_clone.py",
    }
    for name, path in scripts_and_modules.items():
        assert path.exists(), f"{name} is referenced but missing"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for referenced in re.findall(r"python (scripts/[a-z_]+\.py)", readme):
        assert (ROOT / referenced).exists(), f"README references missing script {referenced}"


def test_traceability_matrix_has_no_unproven_required_row() -> None:
    """Every requirement row carries a status, and none of them is a failure.

    The matrix uses two plain-text statuses: ``已验证`` (reproducible evidence) and
    ``受限`` (implemented, but only equivalent evidence is available). A row whose
    status is anything else means a requirement lost its verdict.
    """
    text = (ROOT / "docs" / "requirements_traceability.md").read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| ") and line.count("|") >= 4]
    statuses = [row.rstrip().rstrip("|").split("|")[-1].strip() for row in rows]
    status_rows = [status for status in statuses if status.startswith(("已验证", "受限"))]
    assert len(status_rows) >= 40, "traceability matrix lost rows"
    bad = [
        status for status in statuses if status and not status.startswith(("已验证", "受限", "状态", "---"))
    ]
    assert not bad, f"traceability matrix rows without a verdict: {sorted(set(bad))[:5]}"


def test_documented_suite_counts_match_reality() -> None:
    """Docs quoting 'N pytest' / 'N passed' must not drift from the real suite size."""
    import subprocess

    collected = subprocess.run(
        ["python", "-m", "pytest", "--collect-only", "-q"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    ).stdout
    total = sum(int(count) for count in re.findall(r"^tests/.*: (\d+)$", collected, flags=re.M))
    assert total > 0
    # development_log is a chronological record (it intentionally quotes earlier
    # suite sizes such as "88 passed" as history), so only current-state documents
    # are checked for drift.
    current_docs = [
        ROOT / "README.md",
        ROOT / "docs" / "audit_report.md",
        ROOT / "docs" / "requirements_traceability.md",
        ROOT / "docs" / "api.md",
    ]
    stale: dict[str, list[str]] = {}
    for doc in current_docs:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8")
        for match in re.findall(r"(\d+)\s*(?:个)?\s*(?:pytest|passed|测试)", text):
            if int(match) != total:
                stale.setdefault(str(doc.relative_to(ROOT)), []).append(match)
    assert not stale, f"documents quote stale suite sizes (real size = {total}): {stale}"
