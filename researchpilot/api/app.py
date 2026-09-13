"""FastAPI application: typed, validated, traced REST surface."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from researchpilot import __version__
from researchpilot.api.schemas import (
    DocumentDetail,
    DocumentIngestRequest,
    DocumentIngestResponse,
    DocumentListResponse,
    HealthResponse,
    KnowledgeSearchResponse,
    MetricsResponse,
    ModelConfig,
    RunSummary,
    SourcesResponse,
    SubmitResponse,
    TraceResponse,
)
from researchpilot.api.service import ServiceContainer
from researchpilot.config import Settings, get_settings
from researchpilot.llm.base import BudgetExceededError, LLMConfigError, LLMError
from researchpilot.pipeline import StorageUnavailableError
from researchpilot.rag.retriever import RetrievalResult
from researchpilot.schemas import ResearchRequest, ResearchResult
from researchpilot.utils import utc_now_iso

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("researchpilot.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    container = ServiceContainer(settings)
    app = FastAPI(
        title="ResearchPilot API",
        version=__version__,
        description=(
            "Multi-agent deep research and knowledge base platform: structured agents, "
            "hybrid RAG, tool registry, MCP server, memory, tracing and evaluation."
        ),
    )
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_timing(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.2f}"
        if not request.url.path.startswith("/static"):
            logger.info(
                "%s %s -> %s (%.1f ms)",
                request.method,
                request.url.path,
                response.status_code,
                elapsed,
            )
        return response

    @app.exception_handler(LLMConfigError)
    async def llm_config_handler(request: Request, exc: LLMConfigError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc), "kind": "llm_config"})

    @app.exception_handler(BudgetExceededError)
    async def budget_handler(request: Request, exc: BudgetExceededError) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc), "kind": "token_budget"})

    @app.exception_handler(LLMError)
    async def llm_handler(request: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc), "kind": "llm"})

    @app.exception_handler(StorageUnavailableError)
    async def storage_handler(request: Request, exc: StorageUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc), "kind": "storage_unavailable"})

    @app.exception_handler(OSError)
    async def os_error_handler(request: Request, exc: OSError) -> JSONResponse:
        """Any storage/IO failure must surface as 503, not an unhandled traceback."""
        return JSONResponse(
            status_code=503,
            content={"detail": f"{type(exc).__name__}: {exc}", "kind": "storage_unavailable"},
        )

    # -- meta --------------------------------------------------------------- #
    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    def health() -> HealthResponse:
        return HealthResponse(
            version=__version__,
            provider=settings.provider,
            model=container.provider.model_name(),
            knowledge_base=container.knowledge_base.stats(),
            tools=container.pipeline_tool_names(),
            mcp_transport=container.mcp_mode,
        )

    @app.get("/config", response_model=ModelConfig, tags=["meta"])
    def config() -> ModelConfig:
        return ModelConfig(
            provider=settings.provider,
            models=sorted(settings.price_table().keys()),
            default_model=container.provider.model_name(),
            temperature=settings.temperature,
            presence_penalty=settings.presence_penalty,
            frequency_penalty=settings.frequency_penalty,
            max_tokens=settings.max_tokens,
            top_k=settings.top_k,
            max_iterations=settings.max_iterations,
            token_budget=settings.token_budget,
            embedding_provider=settings.embedding_provider,
            mcp_transport=settings.mcp_transport,
            web_search_mode=settings.web_search_mode,
        )

    # -- research ----------------------------------------------------------- #
    @app.post("/research", response_model=ResearchResult, tags=["research"])
    def create_research(request: ResearchRequest) -> Any:
        if request.mode == "async":
            task_id = container.submit_research(request)
            return JSONResponse(
                status_code=202,
                content=SubmitResponse(
                    task_id=task_id, status="pending", message="poll /research/{task_id}"
                ).model_dump(),
            )
        return container.run_research(request)

    @app.get("/research", response_model=list[RunSummary], tags=["research"])
    def list_research(limit: int = Query(default=20, ge=1, le=100)) -> list[RunSummary]:
        summaries: list[RunSummary] = []
        for result in container.result_store.all(limit=limit):
            summaries.append(
                RunSummary(
                    task_id=result.task_id,
                    question=result.question,
                    status=result.status,
                    created_at=result.created_at,
                    completed_at=result.completed_at,
                    citations=len(result.evidence.sources),
                    latency_ms=result.metrics.latency_ms,
                    errors=len(result.errors),
                )
            )
        return summaries

    @app.get("/research/{task_id}", response_model=ResearchResult, tags=["research"])
    def get_research(task_id: str) -> ResearchResult:
        result = container.result_store.get(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
        return result

    @app.get("/research/{task_id}/trace", response_model=TraceResponse, tags=["research"])
    def get_trace(task_id: str) -> Any:
        trace = container.trace_store.get(task_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"no trace for task_id: {task_id}")
        return trace

    @app.get("/research/{task_id}/sources", response_model=SourcesResponse, tags=["research"])
    def get_sources(task_id: str) -> SourcesResponse:
        result = container.result_store.get(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
        return SourcesResponse(task_id=task_id, sources=result.evidence.sources)

    @app.get("/research/{task_id}/metrics", response_model=MetricsResponse, tags=["research"])
    def get_metrics(task_id: str) -> Any:
        result = container.result_store.get(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown task_id: {task_id}")
        return result.metrics

    # -- knowledge base ----------------------------------------------------- #
    @app.get("/kb/documents", response_model=DocumentListResponse, tags=["knowledge-base"])
    def kb_documents() -> DocumentListResponse:
        kb = container.knowledge_base
        return DocumentListResponse(documents=kb.summaries(), stats=kb.stats())

    @app.get("/kb/documents/{doc_id}", response_model=DocumentDetail, tags=["knowledge-base"])
    def kb_document(doc_id: str) -> DocumentDetail:
        kb = container.knowledge_base
        document = kb.get_document(doc_id)
        if document is None:
            raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")
        chunks = kb.document_chunks(doc_id)
        return DocumentDetail(
            document={
                "doc_id": document.doc_id,
                "title": document.title,
                "source": document.source,
                "kind": document.kind,
                "metadata": document.metadata,
                "created_at": document.created_at,
                "characters": len(document.content),
            },
            chunks=[
                {
                    "chunk_id": chunk.chunk_id,
                    "section": chunk.metadata.get("section", ""),
                    "characters": len(chunk.content),
                    "preview": chunk.content[:240],
                }
                for chunk in chunks
            ],
        )

    @app.get("/kb/search", response_model=KnowledgeSearchResponse, tags=["knowledge-base"])
    def kb_search(
        q: str = Query(min_length=2, max_length=400),
        top_k: int = Query(default=6, ge=1, le=20),
        strategy: str = Query(default="hybrid", pattern="^(dense|keyword|hybrid)$"),
        rewrite: bool = True,
    ) -> KnowledgeSearchResponse:
        started = time.perf_counter()
        result: RetrievalResult = container.knowledge_base.search(
            q, top_k=top_k, strategy=strategy, rewrite=rewrite
        )
        latency = (time.perf_counter() - started) * 1000
        hits = [
            {
                **hit.as_item(),
                "rank": hit.rank,
                "components": hit.components,
                "section": hit.chunk.metadata.get("section", ""),
            }
            for hit in result.hits
        ]
        return KnowledgeSearchResponse(
            query=result.query,
            rewritten_query=result.rewritten_query,
            strategy=result.strategy,
            hits=hits,
            latency_ms=round(latency, 3),
        )

    @app.post("/kb/documents", response_model=DocumentIngestResponse, tags=["knowledge-base"])
    def kb_ingest(payload: DocumentIngestRequest) -> DocumentIngestResponse:
        report = container.knowledge_base.ingest_text(
            payload.content,
            title=payload.title,
            source=payload.source,
            metadata=payload.metadata,
        )
        container.knowledge_base.save()
        return DocumentIngestResponse(**report.model_dump())

    @app.post("/kb/reindex", response_model=DocumentIngestResponse, tags=["knowledge-base"])
    def kb_reindex() -> DocumentIngestResponse:
        report = container.reingest()
        return DocumentIngestResponse(**report.model_dump())

    @app.delete("/kb/documents/{doc_id}", tags=["knowledge-base"])
    def kb_delete(doc_id: str) -> dict[str, Any]:
        removed = container.delete_document(doc_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"unknown doc_id: {doc_id}")
        return {"doc_id": doc_id, "chunks_removed": removed, "stats": container.knowledge_base.stats()}

    # -- MCP ---------------------------------------------------------------- #
    @app.get("/mcp/tools", tags=["mcp"])
    def mcp_tools() -> dict[str, Any]:
        client = container.mcp_client
        return {
            "transport": getattr(client, "name", "unknown"),
            "server": "researchpilot-mcp",
            "tools": client.list_tools() if client is not None else [],
        }

    @app.post("/mcp/call", tags=["mcp"])
    def mcp_call(payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "")
        arguments = payload.get("arguments") or {}
        if not name:
            raise HTTPException(status_code=422, detail="'name' is required")
        client = container.mcp_client
        if client is None:
            raise HTTPException(status_code=503, detail="MCP client is not configured")
        try:
            return client.call_tool(name, arguments)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"MCP call failed: {exc}") from exc

    # -- evaluation --------------------------------------------------------- #
    @app.get("/evaluation/latest", tags=["evaluation"])
    def evaluation_latest() -> dict[str, Any]:
        import json

        candidates = sorted(
            Path("benchmarks").glob("latest_*.json"),
            key=lambda path: path.stat().st_mtime,
        )
        if not candidates:
            return {"available": False, "message": "run scripts/run_benchmark.py first"}
        payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
        return {
            "available": True,
            "generated_at": payload.get("generated_at", utc_now_iso()),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "metrics": payload.get("metrics", {}),
            "categories": payload.get("categories", []),
            "failed_checks": payload.get("failed_checks", {}),
        }

    # -- frontend ----------------------------------------------------------- #
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def index() -> HTMLResponse:
            return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    return app


def container_of(app: FastAPI) -> ServiceContainer:
    container: ServiceContainer = app.state.container
    return container
