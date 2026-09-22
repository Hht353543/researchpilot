"""Research lifecycle and Planner -> Researcher -> Verifier -> Critic -> Writer orchestration."""

from __future__ import annotations

import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from researchpilot.agents.base import ResearchRuntime, build_runtime
from researchpilot.agents.critic import CriticAgent
from researchpilot.agents.planner import PlannerAgent
from researchpilot.agents.researcher import ResearchAgent
from researchpilot.agents.verifier import VerifierAgent
from researchpilot.agents.writer import WriterAgent
from researchpilot.config import Settings, get_settings
from researchpilot.lifecycle import (
    LifecycleCancelled,
    LifecycleInterrupt,
    LifecycleState,
    LifecycleTimedOut,
    TaskLifecycle,
    TerminalState,
)
from researchpilot.llm.base import BudgetExceededError, LLMConfigError, LLMProvider
from researchpilot.llm.factory import build_provider
from researchpilot.observability.trace import Tracer, TraceStore, tracer_scope
from researchpilot.persistence import PersistenceError
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import (
    EvidenceBundle,
    ResearchRequest,
    ResearchResult,
    ResearchSettings,
    ResultQuality,
    TaskMetrics,
    TaskRecord,
    Trace,
)
from researchpilot.security import safe_diagnostic
from researchpilot.store import ResultStore
from researchpilot.task_store import NON_TERMINAL_STATES, TaskStore, process_is_alive
from researchpilot.tools.registry import ToolRegistry
from researchpilot.utils import new_id, utc_now_iso


class StorageUnavailableError(RuntimeError):
    """Raised when a run state cannot be made observable safely."""


class CapacityExceededError(RuntimeError):
    """Raised when this process is already running its configured task limit."""


class PipelineClosedError(RuntimeError):
    """Raised after shutdown has stopped accepting new work."""


class TerminalPersistenceError(StorageUnavailableError):
    """The execution ended, but its terminal task record was not committed."""


@dataclass
class _ManagedTask:
    task_id: str
    request: ResearchRequest
    lifecycle: TaskLifecycle
    result: ResearchResult
    worker: threading.Thread | None = None
    timer: threading.Timer | None = None
    heartbeat_timer: threading.Timer | None = None
    trace: Trace | None = None
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    terminal_published: bool = False
    slot_released: bool = False


def _timestamp_age_seconds(value: str) -> float:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - stamp).total_seconds(), 0.0)


class ResearchPipeline:
    """End-to-end research with a small, managed task state machine."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: LLMProvider | None = None,
        knowledge_base: KnowledgeBase | None = None,
        tools: ToolRegistry | None = None,
        trace_store: TraceStore | None = None,
        result_store: ResultStore | None = None,
        task_store: TaskStore | None = None,
        mcp_client: Any = None,
        tool_overrides: Any = None,
        tool_policy: Any = None,
        owns_provider: bool = False,
        owns_tools: bool = False,
        owns_mcp_client: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider
        self.knowledge_base = knowledge_base or KnowledgeBase.load_or_create(self.settings)
        self.tools = tools
        self.trace_store = trace_store or TraceStore(self.settings.runs_dir())
        self.result_store = result_store or ResultStore(self.settings.runs_dir())
        self.task_store = task_store or TaskStore(self.settings.runs_dir())
        self.mcp_client = mcp_client
        self.tool_overrides = tool_overrides
        self.tool_policy = tool_policy
        self._owns_provider = owns_provider
        self._owns_tools = owns_tools
        self._owns_mcp_client = owns_mcp_client
        self._task_slots = threading.BoundedSemaphore(self.settings.max_concurrent_tasks)
        self._tasks: dict[str, _ManagedTask] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._tasks_lock = threading.RLock()
        self._accepting = True
        self._closed = False
        self._instance_id = new_id("instance")
        self.recovery_count = self.reconcile_startup()

    def _request_settings(self, request: ResearchRequest) -> Settings:
        merged = request.settings
        updates: dict[str, Any] = {}
        if merged.model:
            updates["model"] = merged.model
        for name in (
            "temperature",
            "presence_penalty",
            "frequency_penalty",
            "max_tokens",
            "top_k",
            "max_iterations",
            "token_budget",
        ):
            value = getattr(merged, name)
            if value is not None:
                updates[name] = value
        return self.settings.model_copy(update=updates) if updates else self.settings

    def _build_runtime(
        self,
        task_id: str,
        question: str,
        settings: Settings,
        tracer: Tracer,
        request: ResearchRequest,
        lifecycle: TaskLifecycle | None = None,
    ) -> ResearchRuntime:
        provider = self.provider
        owns_provider = False
        if provider is None:
            provider = build_provider(settings)
            owns_provider = True
        elif settings.model != self.settings.model and provider.name in {"mock", "openai"}:
            provider = build_provider(settings, provider.name)
            owns_provider = True
        mcp_client = self.mcp_client
        if mcp_client is None and settings.mcp_transport == "inprocess":
            from researchpilot.mcp.client import InProcessMcpClient
            from researchpilot.mcp.server import McpServer

            mcp_client = InProcessMcpClient(McpServer(settings, knowledge_base=self.knowledge_base))
        runtime = build_runtime(
            task_id=task_id,
            question=question,
            settings=settings,
            tracer=tracer,
            provider=provider,
            knowledge_base=self.knowledge_base,
            tools=self.tools,
            mcp_client=mcp_client,
            policy=self.tool_policy,
            token_budget_override=request.settings.token_budget,
            max_tokens_override=request.settings.max_tokens,
            lifecycle=lifecycle,
            owns_provider=owns_provider,
            owns_tools=self.tools is None,
        )
        if self.tool_overrides:
            overrides = (
                self.tool_overrides(runtime.tools) if callable(self.tool_overrides) else self.tool_overrides
            )
            for name, tool in overrides.items():
                runtime.tools.register(tool)
                if not runtime.tools.has(name):  # pragma: no cover - defensive
                    raise RuntimeError(f"tool override for unknown tool {name}")
        runtime.scratch["max_sources"] = request.max_sources
        return runtime

    def run(self, request: ResearchRequest, *, task_id: str | None = None) -> ResearchResult:
        record = self._start(request, task_id=task_id)
        wait_s = self.settings.research_task_timeout_s + 1.0
        if not record.done.wait(timeout=wait_s):
            if record.lifecycle.mark_timed_out():
                self._finish(record, "timed_out")
            record.done.wait(timeout=1)
        return record.result

    def submit(self, request: ResearchRequest) -> str:
        return self._start(request).task_id

    def cancel(self, task_id: str) -> bool:
        with self._tasks_lock:
            record = self._tasks.get(task_id)
        if record is None or not record.lifecycle.cancel():
            return False
        self._finish(record, "cancelled")
        return True

    def state(self, task_id: str) -> LifecycleState | None:
        with self._tasks_lock:
            record = self._tasks.get(task_id)
        if record is not None:
            return record.lifecycle.state
        durable = self.task_store.get(task_id)
        return durable.status if durable is not None else None

    def get_result(self, task_id: str) -> ResearchResult | None:
        """Read through the task authority so stale artifacts cannot invent status."""
        task = self.task_store.get(task_id)
        result = self.result_store.get(task_id)
        if task is None:
            return result  # Legacy pre-Phase-4B artifact.
        if result is not None and any(error.startswith("persistence[task]") for error in result.errors):
            return result
        if result is not None and result.status == task.status:
            return result
        if task.status not in NON_TERMINAL_STATES and task.result_state == "complete":
            durable_result = self.result_store.read_durable(task_id)
            if durable_result is not None and durable_result.status == task.status:
                return durable_result
        base = result or ResearchResult(
            task_id=task.task_id,
            trace_id="unavailable",
            question=task.question,
            created_at=task.created_at,
        )
        errors = list(dict.fromkeys([*base.errors, *task.error_summary]))
        return base.model_copy(
            update={
                "status": task.status,
                "quality": None,
                "report": None,
                "errors": errors,
                "completed_at": task.finished_at,
            }
        )

    def list_results(self, *, limit: int = 20) -> list[ResearchResult]:
        ids = {record.task_id for record in self.task_store.all()}
        ids.update(self.result_store.task_ids())
        results = [result for task_id in ids if (result := self.get_result(task_id)) is not None]
        results.sort(key=lambda item: item.created_at, reverse=True)
        return results[:limit]

    def reconcile_startup(self) -> int:
        """Converge abandoned non-terminal records and validate terminal references."""
        self._import_legacy_results()
        with suppress(PersistenceError):
            self.task_store.observe_orphans()
        recovered = 0
        for task in self.task_store.all():
            if task.status in NON_TERMINAL_STATES:
                fresh_owner = process_is_alive(task.owner_pid) and (
                    _timestamp_age_seconds(task.updated_at) <= self.settings.recovery_stale_after_s
                )
                if fresh_owner:
                    continue
                if self._recover_interrupted(task.task_id):
                    recovered += 1
            elif task.status == "completed":
                invalid = self._completed_artifact_is_invalid(task)
                if invalid and self._downgrade_invalid_completion(task.task_id):
                    recovered += 1
                elif not invalid:
                    self._refresh_terminal_artifact_state(task)
            else:
                self._refresh_terminal_artifact_state(task)
        return recovered

    def _import_legacy_results(self) -> None:
        """Give pre-Phase-4B result files task metadata before reconciliation."""
        for task_id in self.result_store.task_ids():
            if self.task_store.get(task_id) is not None:
                continue
            try:
                result = self.result_store.get(task_id)
                if result is None:
                    continue
                try:
                    trace = self.trace_store.get(task_id)
                except PersistenceError:
                    trace = None
                terminal = result.status not in NON_TERMINAL_STATES
                record = TaskRecord(
                    task_id=task_id,
                    question=result.question,
                    status=result.status,
                    created_at=result.created_at or utc_now_iso(),
                    started_at=result.created_at if result.status != "pending" else "",
                    finished_at=result.completed_at if terminal else "",
                    updated_at=result.completed_at or result.created_at or utc_now_iso(),
                    error_summary=result.errors,
                    result_ref=f"{task_id}.result.json",
                    result_state="complete" if terminal else "partial",
                    trace_ref=f"{task_id}.trace.json" if trace is not None else None,
                    trace_state=("complete" if terminal else "partial")
                    if trace is not None
                    else "unavailable",
                )
                self.task_store.create(record)
            except PersistenceError:
                # A concurrent startup may have created the same record. Only
                # suppress that race; real unreadable state remains explicit.
                if self.task_store.get(task_id) is None:
                    continue

    def _recover_interrupted(self, task_id: str) -> bool:
        now = utc_now_iso()

        def recover(current: TaskRecord) -> TaskRecord:
            message = "startup recovery: task owner disappeared before terminal commit"
            errors = list(dict.fromkeys([*current.error_summary, message]))
            result_ref: str | None = None
            result_state = "unavailable"
            try:
                previous = self.result_store.get(task_id)
                failed = (
                    previous
                    or ResearchResult(
                        task_id=task_id,
                        trace_id="unavailable",
                        question=current.question,
                        created_at=current.created_at,
                    )
                ).model_copy(
                    update={
                        "status": "failed",
                        "quality": None,
                        "report": None,
                        "completed_at": now,
                        "errors": errors,
                    }
                )
                self.result_store.save(failed)
                result_ref = f"{task_id}.result.json"
                result_state = "complete"
            except PersistenceError as exc:
                errors.append(f"recovery[result]: {type(exc).__name__}")

            trace_ref: str | None = None
            trace_state = "unavailable"
            try:
                trace = self.trace_store.get(task_id)
                if trace is not None:
                    self.trace_store.save(trace.model_copy(update={"status": "failed", "ended_at": now}))
                    trace_ref = f"{task_id}.trace.json"
                    trace_state = "partial"
            except PersistenceError as exc:
                errors.append(f"recovery[trace]: {type(exc).__name__}")

            return current.model_copy(
                update={
                    "status": "failed",
                    "finished_at": now,
                    "error_summary": errors,
                    "result_ref": result_ref,
                    "result_state": result_state,
                    "trace_ref": trace_ref,
                    "trace_state": trace_state,
                    "recovered_at": now,
                    "recovered_by": self._instance_id,
                }
            )

        for attempt in range(3):
            try:
                _, changed = self.task_store.update(
                    task_id,
                    expected=NON_TERMINAL_STATES,
                    mutate=recover,
                )
                return changed
            except PersistenceError:
                current = self.task_store.get(task_id)
                if current is not None and current.status not in NON_TERMINAL_STATES:
                    return False
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
        return False  # pragma: no cover - loop returns or raises

    def _completed_artifact_is_invalid(self, task: TaskRecord) -> bool:
        if task.result_state != "complete" or task.result_ref != f"{task.task_id}.result.json":
            return True
        try:
            result = self.result_store.get(task.task_id)
        except PersistenceError:
            return True
        return result is None or result.status != "completed"

    def _downgrade_invalid_completion(self, task_id: str) -> bool:
        now = utc_now_iso()

        def downgrade(current: TaskRecord) -> TaskRecord:
            message = "startup recovery: completed task has no valid durable result"
            errors = list(dict.fromkeys([*current.error_summary, message]))
            try:
                previous = self.result_store.get(task_id)
                failed = (
                    previous
                    or ResearchResult(
                        task_id=task_id,
                        trace_id="unavailable",
                        question=current.question,
                        created_at=current.created_at,
                    )
                ).model_copy(
                    update={
                        "status": "failed",
                        "quality": None,
                        "report": None,
                        "completed_at": now,
                        "errors": errors,
                    }
                )
                self.result_store.save(failed)
                result_ref: str | None = f"{task_id}.result.json"
                result_state = "complete"
            except PersistenceError as exc:
                errors.append(f"recovery[result]: {type(exc).__name__}")
                result_ref = None
                result_state = "unavailable"
            return current.model_copy(
                update={
                    "status": "failed",
                    "finished_at": now,
                    "error_summary": errors,
                    "result_ref": result_ref,
                    "result_state": result_state,
                    "recovered_at": now,
                    "recovered_by": self._instance_id,
                }
            )

        _, changed = self.task_store.update(task_id, expected={"completed"}, mutate=downgrade)
        return changed

    def _refresh_terminal_artifact_state(self, task: TaskRecord) -> None:
        updates: dict[str, Any] = {}
        errors = list(task.error_summary)
        if task.result_state == "complete":
            try:
                valid_result = self.result_store.get(task.task_id) is not None
            except PersistenceError:
                valid_result = False
            if not valid_result:
                updates.update(result_ref=None, result_state="unavailable")
                errors.append("startup recovery: result reference is unavailable")
        if task.trace_state in {"complete", "partial"}:
            try:
                valid_trace = self.trace_store.get(task.task_id) is not None
            except PersistenceError:
                valid_trace = False
            if not valid_trace:
                updates.update(trace_ref=None, trace_state="unavailable")
                errors.append("startup recovery: trace reference is unavailable")
        if updates:
            updates["error_summary"] = list(dict.fromkeys(errors))
            self.task_store.update(
                task.task_id,
                expected={task.status},
                mutate=lambda current: current.model_copy(update=updates),
            )

    @property
    def background_task_count(self) -> int:
        with self._tasks_lock:
            return len(self._workers)

    def wait_for_idle(self, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            with self._tasks_lock:
                workers = list(self._workers.values())
            if not workers:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for worker in workers:
                if worker is not threading.current_thread():
                    worker.join(timeout=min(0.05, remaining))

    def request_shutdown(self) -> None:
        with self._tasks_lock:
            self._accepting = False
            task_ids = [task_id for task_id, record in self._tasks.items() if not record.lifecycle.terminal]
        for task_id in task_ids:
            self.cancel(task_id)

    def close(self) -> None:
        with self._tasks_lock:
            if self._closed:
                return
            self._closed = True
        self.request_shutdown()
        if self._owns_provider and self.provider is not None:
            close = getattr(self.provider, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        if self._owns_mcp_client and self.mcp_client is not None:
            close = getattr(self.mcp_client, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        if self._owns_tools and self.tools is not None:
            with suppress(Exception):
                self.tools.close()
        self.wait_for_idle(timeout_s=self.settings.shutdown_timeout_s)

    def _start(self, request: ResearchRequest, *, task_id: str | None = None) -> _ManagedTask:
        with self._tasks_lock:
            if not self._accepting:
                raise PipelineClosedError("research service is shutting down")
        if not self._task_slots.acquire(blocking=False):
            raise CapacityExceededError(
                f"at most {self.settings.max_concurrent_tasks} research tasks may run concurrently"
            )
        task_id = task_id or new_id("task")
        lifecycle = TaskLifecycle(timeout_s=self.settings.research_task_timeout_s)
        created_at = utc_now_iso()
        placeholder = ResearchResult(
            task_id=task_id,
            trace_id="pending",
            status="pending",
            quality=None,
            question=request.question,
            settings=ResearchSettings(**request.settings.model_dump()),
            created_at=created_at,
        )
        record = _ManagedTask(task_id, request, lifecycle, placeholder)
        try:
            self.task_store.create(
                TaskRecord(
                    task_id=task_id,
                    question=request.question,
                    status="pending",
                    created_at=created_at,
                    updated_at=created_at,
                    owner_pid=os.getpid(),
                    owner_instance_id=self._instance_id,
                )
            )
            self.result_store.save(placeholder)
            self.task_store.update(
                task_id,
                expected={"pending"},
                owner_instance_id=self._instance_id,
                mutate=lambda current: current.model_copy(
                    update={
                        "result_ref": f"{task_id}.result.json",
                        "result_state": "partial",
                    }
                ),
            )
        except Exception as exc:
            with suppress(PersistenceError):
                self.task_store.update(
                    task_id,
                    expected=NON_TERMINAL_STATES,
                    owner_instance_id=self._instance_id,
                    mutate=lambda current: current.model_copy(
                        update={
                            "status": "failed",
                            "finished_at": utc_now_iso(),
                            "result_ref": None,
                            "result_state": "unavailable",
                            "error_summary": ["task creation: durable result placeholder failed"],
                        }
                    ),
                )
            self._task_slots.release()
            raise StorageUnavailableError("research task could not be created durably") from exc
        with self._tasks_lock:
            self._tasks[task_id] = record

        worker = threading.Thread(
            target=self._worker_main,
            args=(record,),
            name=f"research-{task_id}",
            daemon=True,
        )
        timer = threading.Timer(lifecycle.remaining(), self._deadline_expired, args=(record,))
        timer.name = f"research-deadline-{task_id}"
        timer.daemon = True
        record.worker = worker
        record.timer = timer
        with self._tasks_lock:
            self._workers[task_id] = worker
        try:
            worker.start()
            timer.start()
            self._schedule_heartbeat(record)
        except Exception:
            with self._tasks_lock:
                self._workers.pop(task_id, None)
                self._tasks.pop(task_id, None)
            if record.lifecycle.cancel():
                self._finish(record, "cancelled")
            raise
        return record

    def _worker_main(self, record: _ManagedTask) -> None:
        try:
            if not record.lifecycle.transition("running"):
                return
            with record.lock:
                if record.lifecycle.state != "running" or record.terminal_published:
                    return
                record.result = record.result.model_copy(update={"status": "running"})
                self.result_store.save(record.result)
                started_at = utc_now_iso()
                self.task_store.update(
                    record.task_id,
                    expected={"pending"},
                    owner_instance_id=self._instance_id,
                    mutate=lambda current: current.model_copy(
                        update={
                            "status": "running",
                            "started_at": started_at,
                            "result_ref": f"{record.task_id}.result.json",
                            "result_state": "partial",
                        }
                    ),
                )
            result, trace = self._run(record.request, task_id=record.task_id, lifecycle=record.lifecycle)
            record.trace = trace
            self._finish(record, cast(TerminalState, result.status), candidate=result, trace=trace)
        except LifecycleCancelled:
            self._finish(record, "cancelled", trace=record.trace)
        except LifecycleTimedOut:
            self._finish(record, "timed_out", trace=record.trace)
        except LifecycleInterrupt:
            return
        except BaseException as exc:
            failed = record.result.model_copy(
                update={
                    "status": "failed",
                    "quality": None,
                    "report": None,
                    "completed_at": utc_now_iso(),
                    "errors": [f"pipeline: {type(exc).__name__}"],
                }
            )
            self._finish(record, "failed", candidate=failed, trace=record.trace)
        finally:
            with self._tasks_lock:
                self._workers.pop(record.task_id, None)
                if record.lifecycle.terminal:
                    self._tasks.pop(record.task_id, None)

    def _deadline_expired(self, record: _ManagedTask) -> None:
        if record.lifecycle.mark_timed_out():
            self._finish(record, "timed_out")

    def _schedule_heartbeat(self, record: _ManagedTask) -> None:
        def heartbeat() -> None:
            if record.lifecycle.terminal or self._closed:
                return
            worker = record.worker
            if worker is None or not worker.is_alive():
                return
            with suppress(PersistenceError):
                self.task_store.update(
                    record.task_id,
                    expected=NON_TERMINAL_STATES,
                    owner_instance_id=self._instance_id,
                    mutate=lambda current: current,
                )
            if not record.lifecycle.terminal and not self._closed:
                timer = threading.Timer(self.settings.task_heartbeat_interval_s, heartbeat)
                timer.name = f"research-heartbeat-{record.task_id}"
                timer.daemon = True
                record.heartbeat_timer = timer
                timer.start()

        if record.lifecycle.terminal or self._closed:
            return
        timer = threading.Timer(self.settings.task_heartbeat_interval_s, heartbeat)
        timer.name = f"research-heartbeat-{record.task_id}"
        timer.daemon = True
        record.heartbeat_timer = timer
        timer.start()

    def _finish(
        self,
        record: _ManagedTask,
        status: TerminalState,
        *,
        candidate: ResearchResult | None = None,
        trace: Trace | None = None,
    ) -> bool:
        with record.lock:
            state = record.lifecycle.state
            if state != status and not record.lifecycle.transition(status):
                return False
            if record.terminal_published:
                return False
            record.terminal_published = True
            trace = trace or record.trace

            if candidate is not None and candidate.status == status:
                result = candidate
            else:
                message = "task cancelled" if status == "cancelled" else "task deadline exceeded"
                result = record.result.model_copy(
                    update={
                        "status": status,
                        "quality": None,
                        "report": None,
                        "completed_at": utc_now_iso(),
                        "errors": [*record.result.errors, message],
                    }
                )
            if status != "completed":
                result = result.model_copy(update={"quality": None, "report": None})
            result.errors = [
                safe_diagnostic(
                    error,
                    secrets=(self.settings.api_key, self.settings.access_token),
                )
                for error in result.errors
            ]

            trace_ref: str | None = None
            trace_state = "unavailable"
            result_ref: str | None = None
            result_state = "unavailable"
            durable_status: TerminalState = status

            def commit_artifacts(current: TaskRecord) -> TaskRecord:
                nonlocal durable_status, result, result_ref, result_state
                nonlocal trace, trace_ref, trace_state
                if trace is not None:
                    trace = trace.model_copy(update={"status": status})
                    try:
                        self.trace_store.save(trace)
                        trace_ref = f"{record.task_id}.trace.json"
                        trace_state = "complete" if status in {"completed", "failed"} else "partial"
                    except Exception as exc:
                        result.errors.append(f"persistence[trace]: {type(exc).__name__}")

                if trace_state == "unavailable" and result.status == "completed":
                    result.quality = "degraded"

                try:
                    self.result_store.save_durable(result)
                    result_ref = f"{record.task_id}.result.json"
                    result_state = "complete"
                except Exception as exc:
                    result.errors.append(f"persistence[result]: {type(exc).__name__}")

                if status == "completed" and result_state != "complete":
                    durable_status = "failed"
                    result = result.model_copy(
                        update={
                            "status": "failed",
                            "quality": None,
                            "report": None,
                        }
                    )
                    if trace is not None and trace_ref is not None:
                        try:
                            trace = trace.model_copy(update={"status": "failed"})
                            self.trace_store.save(trace)
                            trace_state = "partial"
                        except Exception as exc:
                            trace_ref = None
                            trace_state = "unavailable"
                            result.errors.append(f"persistence[trace-reconcile]: {type(exc).__name__}")

                return current.model_copy(
                    update={
                        "status": durable_status,
                        "finished_at": result.completed_at or utc_now_iso(),
                        "error_summary": result.errors,
                        "result_ref": result_ref,
                        "result_state": result_state,
                        "trace_ref": trace_ref,
                        "trace_state": trace_state,
                    }
                )

            task_committed = False
            try:
                _, task_committed = self.task_store.update(
                    record.task_id,
                    expected=NON_TERMINAL_STATES,
                    owner_instance_id=self._instance_id,
                    mutate=commit_artifacts,
                )
            except Exception as exc:
                result.errors.append(f"persistence[task]: {type(exc).__name__}")

            if not task_committed:
                result = result.model_copy(
                    update={
                        "status": "failed",
                        "quality": None,
                        "report": None,
                        "errors": list(
                            dict.fromkeys([*result.errors, "persistence[task]: terminal commit failed"])
                        ),
                    }
                )
            if result_state == "complete" and task_committed:
                self.result_store.publish(result)
            else:
                self.result_store.publish_transient(result)

            record.result = result
            record.trace = trace
            if record.timer is not None:
                record.timer.cancel()
            if record.heartbeat_timer is not None:
                record.heartbeat_timer.cancel()
            if not record.slot_released:
                record.slot_released = True
                self._task_slots.release()
            record.done.set()
            return True

    def _run(
        self,
        request: ResearchRequest,
        *,
        task_id: str,
        lifecycle: TaskLifecycle,
    ) -> tuple[ResearchResult, Trace]:
        settings = self._request_settings(request)
        tracer = Tracer(task_id, request.question, price_table=settings.price_table())
        result = ResearchResult(
            task_id=task_id,
            trace_id=tracer.trace_id,
            status="running",
            quality=None,
            question=request.question,
            settings=ResearchSettings(**request.settings.model_dump()),
            created_at=utc_now_iso(),
        )
        runtime: ResearchRuntime | None = None
        try:
            lifecycle.checkpoint()
            runtime = self._build_runtime(task_id, request.question, settings, tracer, request, lifecycle)
            plan, bundle, verification, critique, report = self._execute(runtime, request, tracer=tracer)
            lifecycle.checkpoint()
            result.plan = plan
            result.evidence = bundle
            result.verification = verification
            result.critique = critique
            result.report = report
            result.errors = [
                safe_diagnostic(
                    error,
                    secrets=(settings.api_key, settings.access_token),
                )
                for error in runtime.errors
            ]
            result.status = "completed"
            result.quality = self._quality(runtime, bundle)
            if report is not None:
                lifecycle.checkpoint()
                try:
                    runtime.memory.persist_task_result(
                        objective=request.question,
                        conclusions=[(c.statement, c.confidence) for c in report.conclusions],
                    )
                except Exception as exc:
                    result.errors.append(f"memory: {type(exc).__name__}")
                    result.quality = "degraded"
        except (LifecycleCancelled, LifecycleTimedOut, LifecycleInterrupt):
            raise
        except BudgetExceededError:
            result.errors.append("budget: task token budget exceeded")
            result.status = "completed"
            result.quality = "degraded"
        except LLMConfigError:
            result.errors.append("llm_config: provider configuration is invalid")
            result.status = "failed"
            result.quality = None
        except Exception as exc:
            result.errors.append(f"pipeline: {type(exc).__name__}")
            result.status = "failed"
            result.quality = None
        finally:
            if runtime is not None:
                self._close_runtime(runtime)

        result.completed_at = utc_now_iso()
        metrics = tracer.metrics()
        result.metrics = _with_iterations(metrics, result.evidence.iterations, result.evidence.tool_calls)
        tracer.finish()
        return result, tracer.to_trace(status=result.status)

    @staticmethod
    def _close_runtime(runtime: ResearchRuntime) -> None:
        if runtime.owns_tools:
            with suppress(Exception):
                runtime.tools.close()
        if runtime.owns_provider:
            close = getattr(runtime.llm.provider, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    def _execute(
        self, runtime: ResearchRuntime, request: ResearchRequest, *, tracer: Tracer
    ) -> tuple[Any, EvidenceBundle, Any, Any, Any]:
        with tracer_scope(tracer):
            return self._run_agents(runtime, request, tracer=tracer)

    def _run_agents(
        self, runtime: ResearchRuntime, request: ResearchRequest, *, tracer: Tracer
    ) -> tuple[Any, EvidenceBundle, Any, Any, Any]:
        planner = PlannerAgent(runtime)
        researcher = ResearchAgent(runtime)
        verifier = VerifierAgent(runtime)
        critic = CriticAgent(runtime)
        writer = WriterAgent(runtime)

        runtime.checkpoint()
        plan = planner.run(request.question, max_iterations=request.settings.max_iterations)
        runtime.checkpoint()
        bundle = researcher.run(plan, iteration=1)
        runtime.checkpoint()
        verification = verifier.run(plan, bundle)
        runtime.checkpoint()
        critique = critic.run(plan, bundle, verification)

        iteration = 1
        max_iterations = max(plan.max_iterations, 1)
        while critique.needs_more_research and critique.follow_up_queries and iteration < max_iterations:
            runtime.checkpoint()
            iteration += 1
            with tracer.span(
                f"pipeline.iteration[{iteration}]",
                "agent",
                agent="Orchestrator",
                input={"follow_up_queries": critique.follow_up_queries},
            ) as span:
                extra = researcher.run(
                    plan,
                    follow_up_queries=critique.follow_up_queries,
                    iteration=iteration,
                )
                tracer.end_span(
                    span,
                    output={
                        "new_evidence": len(extra.evidence),
                        "follow_ups": len(critique.follow_up_queries),
                    },
                )
            bundle = EvidenceBundle(
                evidence=bundle.evidence + extra.evidence,
                sources=list(runtime.sources.all()),
                gaps=bundle.gaps + extra.gaps,
                iterations=iteration,
                tool_calls=bundle.tool_calls + extra.tool_calls,
            )
            runtime.checkpoint()
            verification = verifier.run(plan, bundle)
            runtime.checkpoint()
            critique = critic.run(plan, bundle, verification)

        runtime.checkpoint()
        report = writer.run(plan, bundle, verification, critique.model_dump())
        runtime.checkpoint()
        return plan, bundle, verification, critique, report

    @staticmethod
    def _quality(runtime: ResearchRuntime, bundle: EvidenceBundle) -> ResultQuality:
        if runtime.errors or not bundle.evidence:
            return "degraded"
        return "succeeded"


def _with_iterations(metrics: TaskMetrics, iterations: int, tool_calls: int) -> TaskMetrics:
    return metrics.model_copy(
        update={
            "iterations": iterations,
            "tool_calls": max(metrics.tool_calls, tool_calls),
        }
    )
