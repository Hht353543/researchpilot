"""API request/response models (the same Pydantic models power the agents)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from researchpilot.rag.models import DocumentSummary
from researchpilot.schemas import SourceRef, TaskMetrics, Trace


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    provider: str
    model: str
    knowledge_base: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    mcp_transport: str = "inprocess"
    auth_required: bool = False
    provider_ready: bool = True


class ModelConfig(BaseModel):
    provider: str
    models: list[str]
    default_model: str
    temperature: float
    presence_penalty: float
    frequency_penalty: float
    max_tokens: int
    top_k: int
    max_iterations: int
    token_budget: int
    token_budget_live: int
    embedding_provider: str
    mcp_transport: str
    web_search_mode: str
    auth_required: bool = False
    api_key_configured: bool = False
    max_request_body_bytes: int
    max_concurrent_tasks: int
    research_task_timeout_s: float
    task_heartbeat_interval_s: float
    recovery_stale_after_s: float


class SubmitResponse(BaseModel):
    task_id: str
    status: str
    message: str = ""


class RunSummary(BaseModel):
    task_id: str
    question: str
    status: str
    quality: str | None = None
    created_at: str = ""
    completed_at: str = ""
    citations: int = 0
    latency_ms: float = 0.0
    errors: int = 0


class DocumentIngestRequest(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    content: str = Field(min_length=20, max_length=500_000)
    source: str = "api://inline"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentIngestResponse(BaseModel):
    documents: int
    chunks: int
    characters_in: int
    characters_out: int
    duration_ms: float


class KnowledgeSearchResponse(BaseModel):
    query: str
    rewritten_query: str
    strategy: str
    hits: list[dict[str, Any]]
    # raw retrieval items for the UI table
    latency_ms: float = 0.0


class SourcesResponse(BaseModel):
    task_id: str
    sources: list[SourceRef]


class MetricsResponse(TaskMetrics):
    pass


class TraceResponse(Trace):
    pass


class DocumentDetail(BaseModel):
    document: dict[str, Any]
    chunks: list[dict[str, Any]]


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]
    stats: dict[str, Any]
