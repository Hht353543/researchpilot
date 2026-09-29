"""API persistence failures must never be reported as successful commits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from researchpilot.api.app import create_app
from researchpilot.config import Settings
from researchpilot.persistence import PersistenceError
from researchpilot.rag.vector_store import VectorStore
from researchpilot.schemas import ResearchRequest, ResearchResult


def _fail_vector_save(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_save(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError("simulated failure at C:/private/storage.json")

    monkeypatch.setattr("researchpilot.rag.vector_store.VectorStore.save", fail_save)


async def _client_for(settings: Settings) -> tuple[Any, httpx.AsyncClient]:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _store_state(app: Any) -> dict[str, Any]:
    return app.state.container.knowledge_base.store.to_dict()


def _assert_safe_failure(response: httpx.Response) -> None:
    assert response.status_code == 503
    assert response.json()["kind"] == "persistence_unavailable"
    assert "private" not in response.text.lower()


@pytest.mark.anyio
async def test_kb_add_failure_returns_503_and_rolls_back(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = await _client_for(settings)
    async with client:
        kb = app.state.container.knowledge_base
        before_memory = _store_state(app)
        before_disk = kb.index_path().read_bytes()
        _fail_vector_save(monkeypatch)

        response = await client.post(
            "/kb/documents",
            json={
                "title": "Uncommitted add",
                "source": "failure://add",
                "content": "This document must never become visible when persistence fails.",
            },
        )

        _assert_safe_failure(response)
        assert _store_state(app) == before_memory
        assert kb.index_path().read_bytes() == before_disk
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"

        monkeypatch.undo()
        retry = await client.post(
            "/kb/documents",
            json={
                "title": "Committed retry",
                "source": "failure://retry",
                "content": "A later healthy operation can commit after the failed write.",
            },
        )
        assert retry.status_code == 200
        assert (await client.get("/health")).json()["status"] == "ok"


@pytest.mark.anyio
async def test_kb_update_failure_returns_503_and_restores_old_version(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = await _client_for(settings)
    async with client:
        kb = app.state.container.knowledge_base
        kb.ingest_text("old committed version", title="Mutable", source="failure://update")
        kb.save()
        before_memory = _store_state(app)
        before_disk = kb.index_path().read_bytes()
        _fail_vector_save(monkeypatch)

        response = await client.post(
            "/kb/documents",
            json={
                "title": "Mutable",
                "source": "failure://update",
                "content": "new uncommitted version that must be rolled back",
            },
        )

        _assert_safe_failure(response)
        assert _store_state(app) == before_memory
        assert kb.index_path().read_bytes() == before_disk


@pytest.mark.anyio
async def test_kb_delete_failure_returns_503_and_restores_document(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = await _client_for(settings)
    async with client:
        kb = app.state.container.knowledge_base
        before_memory = _store_state(app)
        before_disk = kb.index_path().read_bytes()
        doc_id = kb.summaries()[0].doc_id
        _fail_vector_save(monkeypatch)

        response = await client.delete(f"/kb/documents/{doc_id}")

        _assert_safe_failure(response)
        assert _store_state(app) == before_memory
        assert kb.index_path().read_bytes() == before_disk


@pytest.mark.anyio
async def test_kb_reindex_failure_returns_503_and_restores_previous_index(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = await _client_for(settings)
    async with client:
        kb = app.state.container.knowledge_base
        kb.ingest_text("API-only committed document", title="API only", source="failure://reindex")
        kb.save()
        before_memory = _store_state(app)
        before_disk = kb.index_path().read_bytes()
        _fail_vector_save(monkeypatch)

        response = await client.post("/kb/reindex")

        _assert_safe_failure(response)
        assert _store_state(app) == before_memory
        assert kb.index_path().read_bytes() == before_disk


def test_failed_kb_save_does_not_advance_committed_generation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    kb = app.state.container.knowledge_base
    committed_generation = kb.store.generation
    kb.ingest_text("candidate generation content", title="Generation", source="failure://generation")
    original_save = VectorStore.save
    _fail_vector_save(monkeypatch)

    with pytest.raises(PersistenceError):
        kb.save()

    assert kb.store.generation == committed_generation
    assert kb.store.generation == kb.__class__.load_or_create(settings).store.generation

    monkeypatch.setattr(VectorStore, "save", original_save)
    kb.ingest_text("committed generation content", title="Generation", source="failure://generation")
    kb.save()
    assert kb.store.generation == committed_generation + 1


@pytest.mark.anyio
async def test_api_health_reports_externally_corrupted_index(
    settings: Settings,
) -> None:
    app, client = await _client_for(settings)
    async with client:
        app.state.container.knowledge_base.index_path().write_text('{"documents":', encoding="utf-8")

        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "persisted state is corrupted",
        "kind": "persistence_corruption",
    }


@pytest.mark.anyio
async def test_sync_research_artifact_failure_returns_503(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = await _client_for(settings)

    def fail_result_save(_result: object) -> None:
        raise PersistenceError("internal result path must not leak")

    monkeypatch.setattr(app.state.container.result_store, "save", fail_result_save)
    async with client:
        response = await client.post(
            "/research",
            json={"question": "How should persistence failures be reported?"},
            timeout=120,
        )

    assert response.status_code == 503
    assert response.json()["kind"] == "storage_unavailable"
    assert "internal result path" not in response.text


def test_failed_terminal_result_is_published_as_failure_and_never_as_completed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(settings)
    container = app.state.container
    original_save = container.result_store.save_durable

    def fail_result_save(result: ResearchResult) -> None:
        if result.status not in {"pending", "running"}:
            raise PersistenceError("simulated result failure")
        original_save(result)

    monkeypatch.setattr(container.result_store, "save_durable", fail_result_save)
    result = container.run_research(
        ResearchRequest(question="How should an asynchronous persistence failure be surfaced?")
    )

    assert result.status == "failed"
    assert result.quality is None
    assert any(error.startswith("persistence[result]") for error in result.errors)
    assert container.result_store.get(result.task_id) is result
    task = container.task_store.get(result.task_id)
    assert task is not None and task.status == "failed"
    assert task.result_state == "unavailable"
    durable = ResearchResult.model_validate_json(
        (settings.runs_dir() / f"{result.task_id}.result.json").read_text(encoding="utf-8")
    )
    assert durable.status == "running", "failed terminal commit must leave the last durable state intact"
