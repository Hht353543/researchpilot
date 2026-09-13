"""FastAPI integration tests (typed endpoints, KB, MCP bridge, error handling)."""

from __future__ import annotations

import httpx
import pytest

from researchpilot.api.app import create_app
from researchpilot.config import Settings


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.mark.anyio
async def test_health_and_config(client: httpx.AsyncClient) -> None:
    health = await client.get("/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["provider"] == "mock"
    assert payload["knowledge_base"]["documents"] >= 1
    assert "knowledge_search" in payload["tools"]

    config = await client.get("/config")
    assert config.status_code == 200
    assert "temperature" in config.json()


@pytest.mark.anyio
async def test_knowledge_base_endpoints(client: httpx.AsyncClient) -> None:
    documents = await client.get("/kb/documents")
    assert documents.status_code == 200
    assert documents.json()["documents"]
    doc_id = documents.json()["documents"][0]["doc_id"]

    detail = await client.get(f"/kb/documents/{doc_id}")
    assert detail.status_code == 200
    assert detail.json()["chunks"]

    search = await client.get("/kb/search", params={"q": "工具注册表", "top_k": 3})
    assert search.status_code == 200
    hits = search.json()["hits"]
    assert hits and hits[0]["source_id"]

    missing = await client.get("/kb/documents/does-not-exist")
    assert missing.status_code == 404

    invalid = await client.get("/kb/search", params={"q": "x"})
    assert invalid.status_code == 422


@pytest.mark.anyio
async def test_ingest_document(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/kb/documents",
        json={
            "title": "测试文档",
            "content": "这是一段用于测试的文档内容，长度足够触发分块逻辑。" * 6,
            "source": "test://inline",
            "metadata": {"topic": "test"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["documents"] == 1
    assert body["chunks"] >= 1


@pytest.mark.anyio
async def test_research_endpoints_and_trace(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/research",
        json={
            "question": "MCP 基于什么协议，核心方法有哪些？",
            "settings": {"temperature": 0.1, "top_k": 3},
        },
        timeout=120,
    )
    assert response.status_code == 200
    result = response.json()
    task_id = result["task_id"]
    assert result["report"]["markdown"]
    assert result["evidence"]["evidence"]

    for path in ("", "/trace", "/sources", "/metrics"):
        fetched = await client.get(f"/research/{task_id}{path}")
        assert fetched.status_code == 200

    trace = (await client.get(f"/research/{task_id}/trace")).json()
    assert trace["spans"]
    assert trace["metrics"]["llm_calls"] >= 1

    metrics = (await client.get(f"/research/{task_id}/metrics")).json()
    assert metrics["tool_calls"] >= 1

    sources = (await client.get(f"/research/{task_id}/sources")).json()
    assert sources["sources"]

    runs = await client.get("/research", params={"limit": 5})
    assert runs.status_code == 200
    assert any(run["task_id"] == task_id for run in runs.json())

    assert (await client.get("/research/unknown-task")).status_code == 404


@pytest.mark.anyio
async def test_async_research_mode(client: httpx.AsyncClient) -> None:
    import asyncio

    response = await client.post(
        "/research",
        json={"question": "混合检索与重排的作用是什么？", "mode": "async"},
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    for _ in range(60):
        fetched = await client.get(f"/research/{task_id}")
        assert fetched.status_code == 200
        if fetched.json()["status"] not in {"pending", "running"}:
            break
        await asyncio.sleep(0.25)
    assert fetched.json()["status"] in {"succeeded", "degraded"}


@pytest.mark.anyio
async def test_invalid_request_is_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.post("/research", json={"question": "x"})).status_code == 422
    assert (
        await client.post("/research", json={"question": "valid", "settings": {"temperature": 5}})
    ).status_code == 422


@pytest.mark.anyio
async def test_mcp_bridge_endpoints(client: httpx.AsyncClient) -> None:
    tools = await client.get("/mcp/tools")
    assert tools.status_code == 200
    names = {tool["name"] for tool in tools.json()["tools"]}
    assert {"search_knowledge", "get_document", "search_web", "get_research_context"} <= names

    call = await client.post(
        "/mcp/call",
        json={"name": "search_knowledge", "arguments": {"query": "工具注册表", "top_k": 2}},
    )
    assert call.status_code == 200
    assert call.json()["structuredContent"]["items"]

    assert (await client.post("/mcp/call", json={"arguments": {}})).status_code == 422


@pytest.mark.anyio
async def test_frontend_is_served(client: httpx.AsyncClient) -> None:
    page = await client.get("/")
    assert page.status_code == 200
    assert "ResearchPilot" in page.text
    script = await client.get("/static/app.js")
    assert script.status_code == 200
