"""Crash recovery and durable task-state commit semantics."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from researchpilot.llm.mock_provider import MockLLMProvider
from researchpilot.persistence import PersistenceError
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest, ResearchResult, TaskRecord
from researchpilot.store import ResultStore
from researchpilot.task_store import TaskStore
from researchpilot.utils import utc_now_iso


def _seed_non_terminal(runs_dir: Path, task_id: str, *, status: str = "running") -> None:
    now = utc_now_iso()
    result = ResearchResult(
        task_id=task_id,
        trace_id="pending",
        status=status,  # type: ignore[arg-type]
        quality=None,
        question="interrupted work",
        created_at=now,
    )
    ResultStore(runs_dir).save(result)
    TaskStore(runs_dir).create(
        TaskRecord(
            task_id=task_id,
            question=result.question,
            status=status,  # type: ignore[arg-type]
            created_at=now,
            started_at=now if status == "running" else "",
            updated_at=now,
            result_ref=f"{task_id}.result.json",
            result_state="partial",
            owner_pid=999_999_999,
            owner_instance_id="dead-owner",
        )
    )


def _pipeline(settings, knowledge_base: KnowledgeBase) -> ResearchPipeline:
    return ResearchPipeline(
        settings=settings,
        provider=MockLLMProvider(settings),
        knowledge_base=knowledge_base,
    )


def test_crashed_process_is_reconciled_on_restart(settings, knowledge_base) -> None:
    runs_dir = settings.runs_dir()
    script = """
import os
import sys
from researchpilot.schemas import ResearchResult, TaskRecord
from researchpilot.store import ResultStore
from researchpilot.task_store import TaskStore
from researchpilot.utils import utc_now_iso

runs_dir = sys.argv[1]
now = utc_now_iso()
task_id = "crash-task"
ResultStore(runs_dir).save(ResearchResult(
    task_id=task_id, trace_id="pending", status="running", quality=None,
    question="crash simulation", created_at=now,
))
TaskStore(runs_dir).create(TaskRecord(
    task_id=task_id, question="crash simulation", status="running",
    created_at=now, started_at=now, updated_at=now,
    result_ref=f"{task_id}.result.json", result_state="partial",
    owner_pid=os.getpid(), owner_instance_id="crashed-child",
))
os._exit(23)
"""
    child = subprocess.run([sys.executable, "-c", script, str(runs_dir)], check=False)
    assert child.returncode == 23

    pipeline = _pipeline(settings, knowledge_base)
    task = pipeline.task_store.get("crash-task")
    assert pipeline.recovery_count == 1
    assert task is not None
    assert task.status == "failed"
    assert task.finished_at and task.recovered_at
    assert task.result_state == "complete"
    assert "owner disappeared" in " ".join(task.error_summary)
    assert pipeline.get_result("crash-task").status == "failed"  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_restart_converges_non_terminal_task(settings, knowledge_base, status: str) -> None:
    task_id = f"restart-{status}"
    _seed_non_terminal(settings.runs_dir(), task_id, status=status)

    pipeline = _pipeline(settings, knowledge_base)

    task = pipeline.task_store.get(task_id)
    assert task is not None and task.status == "failed"
    assert task.recovered_by == pipeline._instance_id
    assert pipeline.get_result(task_id).status == "failed"  # type: ignore[union-attr]


def test_startup_imports_and_recovers_legacy_running_result(settings, knowledge_base) -> None:
    now = utc_now_iso()
    ResultStore(settings.runs_dir()).save(
        ResearchResult(
            task_id="legacy-running",
            trace_id="pending",
            status="running",
            quality=None,
            question="legacy task without metadata",
            created_at=now,
        )
    )

    pipeline = _pipeline(settings, knowledge_base)

    task = pipeline.task_store.get("legacy-running")
    assert pipeline.recovery_count == 1
    assert task is not None and task.status == "failed"
    assert task.recovered_at


def test_two_process_startups_recover_each_task_only_once(settings) -> None:
    _seed_non_terminal(settings.runs_dir(), "single-recovery")
    script = """
import sys
from researchpilot.config import Settings
from researchpilot.pipeline import ResearchPipeline

settings = Settings(
    provider="mock", runs_path=sys.argv[1], kb_path=sys.argv[2], embedding_dim=128,
)
pipeline = ResearchPipeline(settings=settings)
print(pipeline.recovery_count, flush=True)
pipeline.close()
"""
    commands = [[sys.executable, "-c", script, str(settings.runs_dir()), settings.kb_path] for _ in range(2)]
    children = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for command in commands
    ]
    outputs = [child.communicate(timeout=30) for child in children]

    assert all(child.returncode == 0 for child in children), outputs
    assert sum(int(stdout.strip()) for stdout, _stderr in outputs) == 1
    task = TaskStore(settings.runs_dir()).get("single-recovery")
    assert task is not None and task.status == "failed"
    assert task.error_summary.count("startup recovery: task owner disappeared before terminal commit") == 1


def test_startup_preserves_task_with_a_live_worker(settings, knowledge_base, monkeypatch) -> None:
    lease_settings = settings.model_copy(
        update={"task_heartbeat_interval_s": 0.05, "recovery_stale_after_s": 0.15}
    )
    owner = _pipeline(lease_settings, knowledge_base)
    entered = threading.Event()
    release = threading.Event()

    def blocked_run(_request, *, task_id, lifecycle):
        entered.set()
        assert release.wait(timeout=5)
        lifecycle.checkpoint()
        raise AssertionError(f"cancelled task unexpectedly resumed: {task_id}")

    monkeypatch.setattr(owner, "_run", blocked_run)
    task_id = owner.submit(ResearchRequest(question="live worker lease"))
    assert entered.wait(timeout=3)
    time.sleep(0.25)  # Longer than the lease: only heartbeats can keep the owner fresh.

    observer = _pipeline(lease_settings, knowledge_base)
    durable = observer.task_store.get(task_id)
    assert observer.recovery_count == 0
    assert durable is not None and durable.status == "running"

    assert owner.cancel(task_id)
    release.set()
    assert owner.wait_for_idle(timeout_s=5)


def test_recovered_task_rejects_late_worker_artifacts(settings, knowledge_base, monkeypatch) -> None:
    lease_settings = settings.model_copy(
        update={"task_heartbeat_interval_s": 0.05, "recovery_stale_after_s": 0.15}
    )
    owner = _pipeline(lease_settings, knowledge_base)
    entered = threading.Event()
    release = threading.Event()
    original_run = owner._run

    def delayed_run(request, *, task_id, lifecycle):
        entered.set()
        assert release.wait(timeout=5)
        return original_run(request, task_id=task_id, lifecycle=lifecycle)

    monkeypatch.setattr(owner, "_run", delayed_run)
    monkeypatch.setattr(owner, "_schedule_heartbeat", lambda _record: None)
    task_id = owner.submit(ResearchRequest(question="stale worker fencing"))
    assert entered.wait(timeout=3)
    time.sleep(0.25)

    observer = _pipeline(lease_settings, knowledge_base)
    assert observer.recovery_count == 1
    release.set()
    assert owner.wait_for_idle(timeout_s=10)

    task = TaskStore(settings.runs_dir()).get(task_id)
    durable_result = ResultStore(settings.runs_dir()).read_durable(task_id)
    assert task is not None and task.status == "failed"
    assert durable_result is not None and durable_result.status == "failed"


def test_startup_downgrades_completed_task_without_valid_result(settings, knowledge_base) -> None:
    now = utc_now_iso()
    TaskStore(settings.runs_dir()).create(
        TaskRecord(
            task_id="invalid-completion",
            question="missing result",
            status="completed",
            created_at=now,
            started_at=now,
            finished_at=now,
            updated_at=now,
            result_ref="invalid-completion.result.json",
            result_state="complete",
            owner_pid=999_999_999,
            owner_instance_id="dead-owner",
        )
    )

    pipeline = _pipeline(settings, knowledge_base)

    task = pipeline.task_store.get("invalid-completion")
    assert pipeline.recovery_count == 1
    assert task is not None and task.status == "failed"
    assert task.result_state == "complete"
    assert pipeline.get_result("invalid-completion").status == "failed"  # type: ignore[union-attr]


def test_completed_result_failure_is_durable_failure(settings, knowledge_base, monkeypatch) -> None:
    pipeline = _pipeline(settings, knowledge_base)
    original_save = pipeline.result_store.save_durable

    def fail_completed(result: ResearchResult) -> None:
        if result.status == "completed":
            raise PersistenceError("simulated completed-result failure")
        original_save(result)

    monkeypatch.setattr(pipeline.result_store, "save_durable", fail_completed)
    result = pipeline.run(ResearchRequest(question="terminal result durability"))

    task = pipeline.task_store.get(result.task_id)
    assert result.status == "failed"
    assert task is not None and task.status == "failed"
    assert task.result_ref is None
    assert task.result_state == "unavailable"
    assert task.trace_state == "partial"
    assert pipeline.get_result(result.task_id).status == "failed"  # type: ignore[union-attr]


def test_trace_failure_keeps_completed_result_with_explicit_unavailable_trace(
    settings, knowledge_base, monkeypatch
) -> None:
    pipeline = _pipeline(settings, knowledge_base)
    monkeypatch.setattr(
        pipeline.trace_store,
        "save",
        lambda _trace: (_ for _ in ()).throw(PersistenceError("trace unavailable")),
    )

    result = pipeline.run(ResearchRequest(question="partial artifact commit"))
    task = pipeline.task_store.get(result.task_id)

    assert result.status == "completed"
    assert result.quality == "degraded"
    assert task is not None and task.status == "completed"
    assert task.result_state == "complete"
    assert task.trace_ref is None and task.trace_state == "unavailable"


def test_terminal_task_record_failure_never_returns_success(settings, knowledge_base, monkeypatch) -> None:
    pipeline = _pipeline(settings, knowledge_base)
    original_update = pipeline.task_store.update

    def fail_terminal(task_id, *, expected=None, owner_instance_id=None, mutate):
        current = pipeline.task_store.get(task_id)
        assert current is not None
        candidate = mutate(current)
        if candidate.status not in {"pending", "running"}:
            raise PersistenceError("terminal task commit failed")
        return original_update(
            task_id,
            expected=expected,
            owner_instance_id=owner_instance_id,
            mutate=mutate,
        )

    monkeypatch.setattr(pipeline.task_store, "update", fail_terminal)
    result = pipeline.run(ResearchRequest(question="task record durability"))

    durable = TaskStore(settings.runs_dir()).get(result.task_id)
    assert result.status == "failed"
    assert any(error.startswith("persistence[task]") for error in result.errors)
    assert durable is not None and durable.status != "completed"


def test_startup_records_but_does_not_delete_incomplete_temp_files(settings, knowledge_base) -> None:
    runs_dir = settings.runs_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    leftover = runs_dir / ".unknown.result.json.crashed.tmp"
    leftover.write_text("partial", encoding="utf-8")

    _pipeline(settings, knowledge_base)

    report = json.loads((runs_dir / ".recovery-observations.json").read_text(encoding="utf-8"))
    assert leftover.exists()
    assert any(item["path"] == leftover.name for item in report)
