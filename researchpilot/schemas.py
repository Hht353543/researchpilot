"""Typed state for the whole system. Agent state is never a loose string."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from researchpilot.utils import truncate

ToolName = Literal[
    "web_search",
    "knowledge_search",
    "document_reader",
    "calculator",
    "metadata",
    "mcp_research_context",
]
SourceKind = Literal["knowledge_base", "document", "web", "mcp", "computation"]
SpanKind = Literal["task", "agent", "llm", "tool", "retrieval", "retry", "mcp"]
TaskStatus = Literal["pending", "running", "succeeded", "degraded", "failed"]
SubtaskIntent = Literal[
    "knowledge_search",
    "web_search",
    "document_reader",
    "mcp",
    "calculation",
    "synthesis",
]
INTENT_VALUES: tuple[str, ...] = (
    "knowledge_search",
    "web_search",
    "document_reader",
    "mcp",
    "calculation",
    "synthesis",
)
DEFAULT_INTENT: SubtaskIntent = "knowledge_search"

# Checked in order, so a phrase naming two capabilities ("用 MCP 检索知识库")
# resolves to the more specific one. Real models write the intent as a sentence
# rather than the enum value, which used to fail validation outright.
_INTENT_KEYWORDS: tuple[tuple[SubtaskIntent, tuple[str, ...]], ...] = (
    ("mcp", ("mcp", "model context protocol", "tool gateway", "工具网关")),
    ("calculation", ("calculat", "compute", "arithmetic", "计算", "算术", "算一下")),
    (
        "document_reader",
        ("document_reader", "document read", "read the document", "读取文档", "原文", "文档读取"),
    ),
    ("web_search", ("web", "internet", "online", "search the web", "联网", "网页", "网络搜索")),
    ("synthesis", ("synthes", "summar", "conclude", "综合", "汇总", "总结", "归纳", "结论")),
    (
        "knowledge_search",
        ("knowledge", "kb", "retriev", "rag", "vector", "知识库", "检索", "内部资料", "档案"),
    ),
)


def normalize_intent(value: object) -> str:
    """Map a model-written intent to one of :data:`INTENT_VALUES`.

    Providers return anything from the enum value to a full sentence
    ("从内部知识库中确认……"), so the raw text is matched against the vocabulary
    before Pydantic validates it. Unrecognised text falls back to a knowledge
    search, which is what the planner used to do after the fact.
    """
    text = str(value or "").strip().lower()
    if text in INTENT_VALUES:
        return text
    for intent, keywords in _INTENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return intent
    return DEFAULT_INTENT


# --------------------------------------------------------------------------- #
# Request / settings
# --------------------------------------------------------------------------- #
class ResearchSettings(BaseModel):
    """Per-request overrides. ``None`` means "use the server default"."""

    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=64, le=16_000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    max_iterations: int | None = Field(default=None, ge=1, le=4)
    token_budget: int | None = Field(default=None, ge=1_000, le=500_000)

    def merged(self, defaults: ResearchSettings) -> ResearchSettings:
        data = defaults.model_dump()
        data.update({k: v for k, v in self.model_dump().items() if v is not None})
        return ResearchSettings(**data)


class ResearchRequest(BaseModel):
    question: str = Field(min_length=4, max_length=2_000)
    settings: ResearchSettings = Field(default_factory=ResearchSettings)
    mode: Literal["sync", "async"] = "sync"
    max_sources: int = Field(default=8, ge=1, le=30)

    @field_validator("question")
    @classmethod
    def _clean_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question must not be blank")
        return cleaned


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
class Subtask(BaseModel):
    """One decomposed research step."""

    id: str
    question: str
    intent: SubtaskIntent
    tools: list[ToolName] = Field(default_factory=list)
    expected_output: str
    priority: int = Field(default=1, ge=1, le=5)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("intent", mode="before")
    @classmethod
    def _normalise_intent(cls, value: object) -> object:
        """Accept the free-text intent a real model writes, not just the enum."""
        return normalize_intent(value)


class ResearchPlan(BaseModel):
    objective: str
    subtasks: list[Subtask]
    requires_knowledge_base: bool = True
    requires_web: bool = False
    requires_mcp: bool = True
    success_criteria: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=2, ge=1, le=4)
    rationale: str = ""


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
class SourceRef(BaseModel):
    id: str
    kind: SourceKind
    title: str
    locator: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    url: str = ""
    retrieved_at: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A single verifiable statement bound to a source."""

    id: str
    subtask_id: str = ""
    claim: str
    quote: str
    source_id: str
    tool: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("quote")
    @classmethod
    def _quote_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence quote must not be empty")
        return value.strip()


class EvidenceBundle(BaseModel):
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    iterations: int = 1
    tool_calls: int = 0

    def by_id(self, evidence_id: str) -> Evidence | None:
        return next((e for e in self.evidence if e.id == evidence_id), None)


# --------------------------------------------------------------------------- #
# Verification / critique
# --------------------------------------------------------------------------- #
class CitationCheck(BaseModel):
    evidence_id: str
    statement: str
    status: Literal["supported", "weak", "unsupported"]
    reason: str = ""
    overlap: float = Field(default=0.0, ge=0.0, le=1.0)


class VerificationReport(BaseModel):
    checks: list[CitationCheck] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    flagged_sources: list[str] = Field(
        default_factory=list,
        description="sources that carry prompt-injection / low-quality signals",
    )
    source_quality: dict[str, float] = Field(default_factory=dict)
    coverage_score: float = Field(default=0.0, ge=0.0, le=1.0)
    sufficient: bool = False
    missing_topics: list[str] = Field(default_factory=list)
    overall_score: float = Field(default=0.0, ge=0.0, le=1.0)


class CritiqueIssue(BaseModel):
    severity: Literal["low", "medium", "high"]
    category: Literal["coverage", "logic", "citation", "recency", "redundancy", "safety"]
    description: str
    suggested_fix: str = ""


class CritiqueReport(BaseModel):
    issues: list[CritiqueIssue] = Field(default_factory=list)
    needs_more_research: bool = False
    follow_up_queries: list[str] = Field(default_factory=list)
    coverage: dict[str, float] = Field(default_factory=dict)
    overall_assessment: str = ""


# --------------------------------------------------------------------------- #
# Retrieval helpers
# --------------------------------------------------------------------------- #
class RerankScore(BaseModel):
    id: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class RerankScores(BaseModel):
    scores: list[RerankScore] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
class ReportClaim(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ReportSection(BaseModel):
    heading: str
    body: str
    evidence_ids: list[str] = Field(default_factory=list)


class FinalReport(BaseModel):
    title: str
    executive_summary: str
    conclusions: list[ReportClaim] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    dropped_citations: list[str] = Field(default_factory=list)
    markdown: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_findings_sections(cls, data: Any) -> Any:
        """Accept the section shape a real model writes.

        deepseek-chat answered with ``findings[].narrative`` (its reading of the
        prompt's "findings" structure) while the schema expects ``sections``.
        Pydantic drops unknown keys and keeps the defaults, so the report validated
        with empty lists and the writer replaced it with the extractive fallback.
        Here the findings are folded into ``sections``, which keeps the model's own
        text and its citation ids.
        """
        if not isinstance(data, dict):
            return data
        findings = data.get("findings")
        if not isinstance(findings, list) or not findings:
            return data
        sections = list(data.get("sections") or [])
        if sections:
            return data
        supplied_conclusions = bool(data.get("conclusions"))
        conclusions = list(data.get("conclusions") or [])
        for item in findings:
            if not isinstance(item, dict):
                continue
            body = str(item.get("narrative") or item.get("body") or item.get("statement") or "").strip()
            if not body:
                continue
            heading = str(item.get("heading") or item.get("title") or "分析").strip()
            evidence_ids = list(item.get("evidence_ids") or [])
            sections.append(
                {
                    "heading": heading,
                    "body": body,
                    "evidence_ids": evidence_ids,
                }
            )
            if not supplied_conclusions:
                # The model wrote its claims as findings; without this the report
                # renders no conclusions at all and the executive view is lost.
                conclusions.append(
                    {
                        "statement": truncate(body, 400),
                        "evidence_ids": evidence_ids,
                        "confidence": float(item.get("confidence") or 0.5),
                    }
                )
        if not sections:
            return data
        merged = dict(data)
        merged["sections"] = sections
        if conclusions:
            merged["conclusions"] = conclusions
        merged.pop("findings", None)
        return merged


# --------------------------------------------------------------------------- #
# Observability
# --------------------------------------------------------------------------- #
class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 8),
        )


class Span(BaseModel):
    span_id: str
    trace_id: str
    parent_id: str | None = None
    name: str
    kind: SpanKind
    agent: str = ""
    tool: str = ""
    model: str = ""
    start_time: str
    end_time: str = ""
    latency_ms: float = 0.0
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskMetrics(BaseModel):
    latency_ms: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    retrieval_calls: int = 0
    retries: int = 0
    mcp_calls: int = 0
    iterations: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    agent_latency_ms: dict[str, float] = Field(default_factory=dict)


class Trace(BaseModel):
    trace_id: str
    task_id: str
    question: str
    spans: list[Span] = Field(default_factory=list)
    started_at: str
    ended_at: str = ""
    metrics: TaskMetrics = Field(default_factory=TaskMetrics)

    def spans_of(self, kind: SpanKind) -> list[Span]:
        return [s for s in self.spans if s.kind == kind]


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #
class ResearchResult(BaseModel):
    task_id: str
    trace_id: str
    status: TaskStatus = "succeeded"
    question: str
    settings: ResearchSettings = Field(default_factory=ResearchSettings)
    plan: ResearchPlan | None = None
    evidence: EvidenceBundle = Field(default_factory=EvidenceBundle)
    verification: VerificationReport | None = None
    critique: CritiqueReport | None = None
    report: FinalReport | None = None
    metrics: TaskMetrics = Field(default_factory=TaskMetrics)
    errors: list[str] = Field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""
