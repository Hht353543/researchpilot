"""First-run, configuration publication and uploaded-document persistence."""

from __future__ import annotations

import base64
from io import BytesIO

import httpx
import pytest

from researchpilot.api.app import create_app
from researchpilot.desktop.api import ConnectionFailure, DesktopController
from researchpilot.desktop.settings import DesktopStore
from tests.fixtures.openai_stub import run_stub_server
from tests.unit.test_desktop import TestProtector


@pytest.fixture
def desktop_app(tmp_path):
    store = DesktopStore(tmp_path, protector=TestProtector())
    store.prepare()
    app = create_app(store.runtime_settings(), desktop=DesktopController(store, instance_id="test-instance"))
    yield app, store
    app.state.container.close()


def desktop_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"X-ResearchPilot": "desktop"},
    )


@pytest.mark.anyio
async def test_no_key_boots_and_research_cannot_use_mock(desktop_app):
    app, store = desktop_app
    async with desktop_client(app) as client:
        assert (await client.get("/")).status_code == 200
        health = (await client.get("/health")).json()
        assert health["desktop_mode"] and not health["provider_ready"]
        response = await client.post("/research", json={"question": "什么是 RAG？", "mode": "async"})
        assert response.status_code == 503
        assert (await client.get("/research")).json() == []
        assert not store.api_key


@pytest.mark.anyio
async def test_save_is_hot_applied_and_clear_disables_research(desktop_app, monkeypatch):
    app, store = desktop_app
    monkeypatch.setattr("researchpilot.desktop.api.probe_connection", lambda config, key: None)
    async with desktop_client(app) as client:
        response = await client.put("/desktop/settings", json={"api_key": "private-test-key", "top_k": 3})
        assert response.status_code == 200
        assert "private-test-key" not in response.text
        config = (await client.get("/config")).json()
        assert config["top_k"] == 3 and config["api_key_configured"]
        assert (await client.get("/health")).json()["provider_ready"]
        assert store.api_key == app.state.container.settings.api_key == "private-test-key"
        assert (await client.delete("/desktop/settings/api-key")).status_code == 200
        assert not (await client.get("/health")).json()["provider_ready"]


@pytest.mark.anyio
async def test_failed_validation_and_failed_save_keep_previous_config(desktop_app, monkeypatch):
    app, store = desktop_app
    monkeypatch.setattr("researchpilot.desktop.api.probe_connection", lambda config, key: None)
    async with desktop_client(app) as client:
        assert (await client.put("/desktop/settings", json={"api_key": "old-key"})).status_code == 200
        previous = store.config_path.read_text()

        def invalid(config, key):
            raise ConnectionFailure("invalid_api_key", "API Key 无效。")

        monkeypatch.setattr("researchpilot.desktop.api.probe_connection", invalid)
        response = await client.put("/desktop/settings", json={"api_key": "new-key", "top_k": 2})
        assert response.status_code == 400 and "new-key" not in response.text
        monkeypatch.setattr("researchpilot.desktop.api.probe_connection", lambda config, key: None)

        def unavailable(config, key):
            raise OSError("write failed")

        monkeypatch.setattr(store, "save", unavailable)
        assert (await client.put("/desktop/settings", json={"api_key": "new-key"})).status_code == 503
        assert store.config_path.read_text() == previous
        assert app.state.container.settings.api_key == store.api_key == "old-key"


@pytest.mark.anyio
async def test_validation_errors_do_not_echo_secrets(desktop_app):
    app, _ = desktop_app
    async with desktop_client(app) as client:
        response = await client.put("/desktop/settings", json={"api_key": "secret\nvalue", "top_k": -1})
        assert response.status_code == 422
        assert "secret" not in response.text and "value" not in response.text


@pytest.mark.anyio
async def test_browser_origin_and_host_boundary(desktop_app):
    app, _ = desktop_app
    async with desktop_client(app) as client:
        assert (
            await client.get("/desktop/settings", headers={"Origin": "https://other.example"})
        ).status_code == 403
        assert (await client.get("/desktop/settings", headers={"Host": "other.example"})).status_code == 403
        assert (
            await client.delete("/desktop/settings/api-key", headers={"X-ResearchPilot": ""})
        ).status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize(
    "filename,content",
    [
        ("test.txt", "企业知识库让团队共享可靠资料，引用能够追溯来源。"),
        ("test.md", "# 企业知识库\n企业知识库让团队共享可靠资料，引用能够追溯来源。"),
        ("test.csv", "title,content\n测试,企业知识库让团队共享可靠资料，引用能够追溯来源。"),
        ("test.json", '[{"title":"测试","content":"企业知识库让团队共享可靠资料，引用能够追溯来源。"}]'),
        ("test.jsonl", '{"title":"测试","content":"企业知识库让团队共享可靠资料，引用能够追溯来源。"}'),
    ],
)
async def test_files_remain_after_rebuild_and_restart(desktop_app, filename, content):
    app, store = desktop_app
    async with desktop_client(app) as client:
        assert (
            await client.post("/kb/import", json={"filename": filename, "content": content})
        ).status_code == 200
        documents = (await client.get("/kb/documents")).json()["documents"]
        imported = next(doc for doc in documents if doc["doc_id"].startswith("import_"))
        assert (await client.post("/kb/reindex")).status_code == 200
        assert app.state.container.knowledge_base.get_document(imported["doc_id"])
        restored = create_app(store.runtime_settings(), desktop=DesktopController(store))
        try:
            assert restored.state.container.knowledge_base.get_document(imported["doc_id"])
        finally:
            restored.state.container.close()
        assert (await client.delete(f"/kb/documents/{imported['doc_id']}")).status_code == 200
        assert (await client.post("/kb/reindex")).status_code == 200
        assert not app.state.container.knowledge_base.get_document(imported["doc_id"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "filename,content",
    [("../outside.txt", "hello"), ("bad.exe", "hello"), ("bad.json", "{"), ("empty.txt", " ")],
)
async def test_invalid_files_are_rejected(desktop_app, filename, content):
    app, _ = desktop_app
    async with desktop_client(app) as client:
        assert (
            await client.post("/kb/import", json={"filename": filename, "content": content})
        ).status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", ["pdf", "docx"])
async def test_binary_import_survives_restart(desktop_app, suffix):
    from docx import Document

    from tests.unit.test_personal_research import text_pdf

    if suffix == "pdf":
        from pypdf import PdfWriter

        writer, buffer = PdfWriter(clone_from=BytesIO(text_pdf())), BytesIO()
        writer.add_attachment("附件.bin", b"x" * 1_100_000)
        writer.write(buffer)
        data = buffer.getvalue()
        expected = "not supported"
    else:
        document, buffer = Document(), BytesIO()
        document.add_paragraph("个人产品不支持离线部署，收费 10 元。")
        document.save(buffer)
        data, expected = buffer.getvalue(), "不支持离线部署"
    app, store = desktop_app
    async with desktop_client(app) as client:
        response = await client.post(
            "/kb/import",
            json={"filename": f"产品说明.{suffix}", "content_base64": base64.b64encode(data).decode()},
        )
        assert response.status_code == 200, response.text
        imported = (await client.get("/kb/documents")).json()["documents"][0]
        restored = create_app(store.runtime_settings(), desktop=DesktopController(store))
        try:
            loaded = restored.state.container.knowledge_base.get_document(imported["doc_id"])
            assert loaded and expected in loaded.content and not loaded.metadata.get("example")
        finally:
            restored.state.container.close()


@pytest.mark.anyio
async def test_examples_are_opt_in_and_cannot_be_selected_as_personal_documents(desktop_app, monkeypatch):
    app, _ = desktop_app
    monkeypatch.setattr("researchpilot.desktop.api.probe_connection", lambda config, key: None)
    async with desktop_client(app) as client:
        assert (await client.get("/kb/documents")).json()["documents"] == []
        assert (await client.put("/desktop/settings", json={"api_key": "test-key"})).status_code == 200
        assert (await client.post("/kb/examples")).status_code == 200
        samples = (await client.get("/kb/documents")).json()["documents"]
        assert samples and all(doc["metadata"].get("example") for doc in samples)
        response = await client.post("/research", json={"question": "资料有哪些？", "mode": "async"})
        assert response.status_code == 422 and response.json()["kind"] == "document_scope"
        response = await client.post(
            "/research", json={"question": "资料有哪些？", "document_ids": [samples[0]["doc_id"]]}
        )
        assert response.status_code == 422


@pytest.mark.anyio
async def test_settings_cannot_forward_old_key_to_another_service(desktop_app):
    app, store = desktop_app
    store.api_key = "do-not-forward"
    async with desktop_client(app) as client:
        response = await client.put(
            "/desktop/settings", json={"service": "custom", "base_url": "https://other.example"}
        )
        assert response.status_code == 422 and "do-not-forward" not in response.text


@pytest.mark.anyio
async def test_real_http_setup_and_first_research(desktop_app):
    app, _ = desktop_app
    with run_stub_server() as (base_url, state):
        async with desktop_client(app) as client:
            update = {
                "service": "custom",
                "base_url": base_url,
                "model": "gpt-4o-mini",
                "api_key": "test-key",
                "max_iterations": 1,
            }
            assert (await client.put("/desktop/settings", json=update)).status_code == 200
            assert (await client.post("/kb/examples")).status_code == 200
            result = (
                await client.post(
                    "/research", json={"question": "RAG 知识库如何帮助企业研究？", "use_examples": True}
                )
            ).json()
            assert result["status"] == "completed" and result["report"]["markdown"]
            assert result["evidence"]["sources"] and state.chat_requests > 1


@pytest.mark.anyio
async def test_active_research_blocks_configuration_change(desktop_app):
    app, _ = desktop_app
    with run_stub_server(latency_s=0.2) as (base_url, _state):
        async with desktop_client(app) as client:
            update = {
                "service": "custom",
                "base_url": base_url,
                "model": "gpt-4o-mini",
                "api_key": "test-key",
            }
            assert (await client.put("/desktop/settings", json=update)).status_code == 200
            assert (await client.post("/kb/examples")).status_code == 200
            submission = await client.post(
                "/research", json={"question": "分析 RAG 知识库", "mode": "async", "use_examples": True}
            )
            assert submission.status_code == 202
            assert (await client.put("/desktop/settings", json=update)).status_code == 409
            assert (await client.delete("/desktop/settings/api-key")).status_code == 409
            await client.delete("/research/" + submission.json()["task_id"])
