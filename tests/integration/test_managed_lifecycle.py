"""Managed research tasks terminate once and release process-owned resources."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest

from researchpilot.api.app import create_app
from researchpilot.config import Settings
from researchpilot.llm.base import ChatMessage, LLMProvider, LLMResponse
from researchpilot.pipeline import ResearchPipeline
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.schemas import ResearchRequest


class BlockingProvider(LLMProvider):
    name = "blocking"

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.closed = False

    def model_name(self) -> str:
        return "blocking"

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        response_schema: type[Any] | None = None,
        hints: dict[str, Any] | None = None,
        purpose: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
    ) -> LLMResponse:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        raise RuntimeError("provider should not schedule another call")

    def close(self) -> None:
        self.closed = True


def _wait_for_status(pipeline: ResearchPipeline, task_id: str, status: str) -> Any:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = pipeline.result_store.get(task_id)
        if result is not None and result.status == status:
            return result
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} never reached {status}")


def test_async_success_is_completed_and_releases_worker(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    pipeline = ResearchPipeline(settings=settings, knowledge_base=knowledge_base)
    task_id = pipeline.submit(ResearchRequest(question="Explain the tool registry lifecycle"))
    result = _wait_for_status(pipeline, task_id, "completed")

    assert result.quality in {"succeeded", "degraded"}
    assert result.report is not None
    assert pipeline.trace_store.get(task_id).status == "completed"  # type: ignore[union-attr]
    task = pipeline.task_store.get(task_id)
    assert task is not None and task.status == "completed"
    assert task.result_state == "complete" and task.trace_state == "complete"
    pipeline.wait_for_idle(timeout_s=5)
    assert pipeline.background_task_count == 0
    pipeline.close()


def test_cancel_is_terminal_stops_new_calls_and_releases_capacity(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    bounded = settings.model_copy(update={"max_concurrent_tasks": 1, "research_task_timeout_s": 10})
    provider = BlockingProvider()
    pipeline = ResearchPipeline(
        settings=bounded,
        provider=provider,
        knowledge_base=knowledge_base,
        owns_provider=True,
    )
    task_id = pipeline.submit(ResearchRequest(question="Block until cancellation"))
    assert provider.entered.wait(timeout=3)

    assert pipeline.cancel(task_id)
    cancelled = _wait_for_status(pipeline, task_id, "cancelled")
    assert cancelled.report is None
    task = pipeline.task_store.get(task_id)
    assert task is not None and task.status == "cancelled"
    calls_at_cancel = provider.calls

    provider.release.set()
    pipeline.wait_for_idle(timeout_s=5)
    time.sleep(0.05)
    assert pipeline.result_store.get(task_id).status == "cancelled"  # type: ignore[union-attr]
    assert provider.calls == calls_at_cancel

    second_id = pipeline.submit(ResearchRequest(question="Capacity is available again"))
    assert second_id != task_id
    assert pipeline.cancel(second_id)
    pipeline.close()
    assert provider.closed


def test_wall_clock_timeout_is_terminal_and_late_work_cannot_complete(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    bounded = settings.model_copy(update={"research_task_timeout_s": 0.1})
    provider = BlockingProvider()
    pipeline = ResearchPipeline(
        settings=bounded,
        provider=provider,
        knowledge_base=knowledge_base,
        owns_provider=True,
    )
    task_id = pipeline.submit(ResearchRequest(question="Exceed the whole-task deadline"))
    assert provider.entered.wait(timeout=3)

    result = _wait_for_status(pipeline, task_id, "timed_out")
    assert result.report is None
    task = pipeline.task_store.get(task_id)
    assert task is not None and task.status == "timed_out"
    second_id = pipeline.submit(ResearchRequest(question="Timeout released the capacity slot"))
    assert pipeline.cancel(second_id)
    provider.release.set()
    pipeline.wait_for_idle(timeout_s=5)
    assert pipeline.result_store.get(task_id).status == "timed_out"  # type: ignore[union-attr]
    pipeline.close()


def test_unhandled_execution_failure_is_safe_terminal_and_releases_capacity(
    settings: Settings, knowledge_base: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    bounded = settings.model_copy(update={"max_concurrent_tasks": 1})
    pipeline = ResearchPipeline(settings=bounded, knowledge_base=knowledge_base)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("secret-token at C:/private/provider.log")

    monkeypatch.setattr(pipeline, "_execute", fail)
    result = pipeline.run(ResearchRequest(question="Fail inside the pipeline"))
    assert result.status == "failed"
    assert result.quality is None
    assert result.report is None
    assert "secret-token" not in " ".join(result.errors)
    assert "private" not in " ".join(result.errors)
    trace = pipeline.trace_store.get(result.task_id)
    assert trace is not None and trace.status == "failed"
    task = pipeline.task_store.get(result.task_id)
    assert task is not None and task.status == "failed"

    second = pipeline.submit(ResearchRequest(question="Failure released the slot"))
    assert pipeline.cancel(second)
    pipeline.close()


def test_task_owned_resources_close_but_shared_provider_survives_task(
    settings: Settings, knowledge_base: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from researchpilot.tools.registry import ToolRegistry

    task_provider = BlockingProvider()
    task_provider.release.set()
    monkeypatch.setattr("researchpilot.pipeline.build_provider", lambda _settings: task_provider)
    closes: list[ToolRegistry] = []
    original_close = ToolRegistry.close

    def record_close(registry: ToolRegistry) -> None:
        closes.append(registry)
        original_close(registry)

    monkeypatch.setattr(ToolRegistry, "close", record_close)
    task_owned = ResearchPipeline(settings=settings, knowledge_base=knowledge_base)
    task_owned.run(ResearchRequest(question="Task-owned resources close"))
    assert task_provider.closed
    assert closes

    shared = BlockingProvider()
    shared.release.set()
    shared_pipeline = ResearchPipeline(
        settings=settings,
        provider=shared,
        knowledge_base=knowledge_base,
        owns_provider=False,
    )
    shared_pipeline.run(ResearchRequest(question="Shared provider stays open"))
    assert not shared.closed
    shared_pipeline.close()
    assert not shared.closed


def test_shutdown_cancels_tasks_and_rejects_new_work(
    settings: Settings, knowledge_base: KnowledgeBase
) -> None:
    from researchpilot.pipeline import PipelineClosedError

    provider = BlockingProvider()
    pipeline = ResearchPipeline(settings=settings, provider=provider, knowledge_base=knowledge_base)
    task_id = pipeline.submit(ResearchRequest(question="Shutdown owns this task"))
    assert provider.entered.wait(timeout=3)
    pipeline.request_shutdown()
    assert _wait_for_status(pipeline, task_id, "cancelled").status == "cancelled"
    with pytest.raises(PipelineClosedError):
        pipeline.submit(ResearchRequest(question="No work after shutdown"))
    provider.release.set()
    assert pipeline.wait_for_idle(timeout_s=5)
    pipeline.close()


def test_cancel_cannot_be_overwritten_by_a_late_running_publication(
    settings: Settings, knowledge_base: KnowledgeBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = ResearchPipeline(settings=settings, knowledge_base=knowledge_base)
    running_save_entered = threading.Event()
    allow_running_save = threading.Event()
    original_save = pipeline.result_store.save

    def controlled_save(result: Any) -> None:
        if result.status == "running":
            running_save_entered.set()
            assert allow_running_save.wait(timeout=5)
        original_save(result)

    monkeypatch.setattr(pipeline.result_store, "save", controlled_save)
    task_id = pipeline.submit(ResearchRequest(question="Cancel during running publication"))
    assert running_save_entered.wait(timeout=3)
    cancelled: list[bool] = []
    cancel_thread = threading.Thread(target=lambda: cancelled.append(pipeline.cancel(task_id)))
    cancel_thread.start()
    allow_running_save.set()
    cancel_thread.join(timeout=3)

    assert cancelled == [True]
    assert _wait_for_status(pipeline, task_id, "cancelled").status == "cancelled"
    pipeline.wait_for_idle(timeout_s=5)
    assert pipeline.result_store.get(task_id).status == "cancelled"  # type: ignore[union-attr]
    pipeline.close()


@pytest.mark.anyio
async def test_cancel_endpoint_and_application_shutdown(settings: Settings) -> None:
    bounded = settings.model_copy(update={"research_task_timeout_s": 10})
    app = create_app(bounded)
    provider = BlockingProvider()
    app.state.container.provider = provider
    app.state.container.pipeline.provider = provider
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        submitted = await client.post(
            "/research", json={"question": "Cancel through the API", "mode": "async"}
        )
        task_id = submitted.json()["task_id"]
        assert provider.entered.wait(timeout=3)
        response = await client.delete(f"/research/{task_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

    provider.release.set()
    app.state.container.close()
    assert provider.closed


@pytest.mark.anyio
async def test_fastapi_lifespan_closes_container_and_stops_admission(settings: Settings) -> None:
    from researchpilot.pipeline import PipelineClosedError

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.container.pipeline._accepting
    assert not app.state.container.pipeline._accepting
    with pytest.raises(PipelineClosedError):
        app.state.container.submit_research(ResearchRequest(question="Rejected after shutdown"))
