"""MCP server exposing knowledge-base, document, web and research-context tools."""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

# Imported at module level (not inside create_http_app) so FastAPI can resolve the
# response annotation when generating /openapi.json and /docs: with
# ``from __future__ import annotations`` a function-local import leaves an
# unresolvable ForwardRef and OpenAPI generation raises PydanticUserError.
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from researchpilot.config import Settings, get_settings
from researchpilot.mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
    JsonRpcRequest,
    McpToolSpec,
    failure,
    initialize_result,
    success,
    text_content,
)
from researchpilot.persistence import PersistenceError
from researchpilot.rag.knowledge_base import KnowledgeBase
from researchpilot.tools.web_backend import build_web_backend
from researchpilot.utils import truncate


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    strategy: str = Field(default="hybrid", pattern="^(dense|keyword|hybrid)$")
    filters: dict[str, Any] = Field(default_factory=dict)


class GetDocumentArgs(BaseModel):
    doc_id: str = Field(default="", max_length=200)
    chunk_id: str = Field(default="", max_length=250)
    max_chars: int = Field(default=6000, ge=200, le=40_000)


class SearchWebArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    top_k: int = Field(default=5, ge=1, le=20)


class ResearchContextArgs(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    subtask: str = Field(default="", max_length=300)
    top_k: int = Field(default=5, ge=1, le=20)


class McpServer:
    """Transport-independent MCP server implementation."""

    def __init__(
        self, settings: Settings | None = None, *, knowledge_base: KnowledgeBase | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_knowledge_base = knowledge_base is None
        self.knowledge_base = knowledge_base or KnowledgeBase.load_or_create(self.settings)
        self.web_backend = build_web_backend(self.settings)
        self._tools: dict[
            str, tuple[McpToolSpec, type[BaseModel], Callable[[BaseModel], dict[str, Any]]]
        ] = {}
        self._register_tools()
        self.calls: dict[str, int] = {}

    def close(self) -> None:
        if getattr(self, "_owns_knowledge_base", False):
            self.knowledge_base.close()

    def _register_tools(self) -> None:
        self._add(
            "search_knowledge",
            "Hybrid (dense + BM25) search over the internal knowledge base, with optional "
            "metadata filters and reranking. Returns quotable chunks with source ids.",
            SearchKnowledgeArgs,
            self._search_knowledge,
        )
        self._add(
            "get_document",
            "Fetch a full knowledge-base document or a single chunk by id.",
            GetDocumentArgs,
            self._get_document,
        )
        self._add(
            "search_web",
            "Search the configured web backend (bundled offline corpus by default).",
            SearchWebArgs,
            self._search_web,
        )
        self._add(
            "get_research_context",
            "Packaged research context for a sub-task: knowledge-base hits, web references "
            "and coverage metadata.",
            ResearchContextArgs,
            self._research_context,
        )

    def _add(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        handler: Callable[[BaseModel], dict[str, Any]],
    ) -> None:
        spec = McpToolSpec(name=name, description=description, inputSchema=args_model.model_json_schema())
        self._tools[name] = (spec, args_model, handler)

    # -- JSON-RPC surface --------------------------------------------------- #
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        try:
            request = JsonRpcRequest.model_validate(message)
        except Exception as exc:
            return failure(request_id, JsonRpcError(INVALID_REQUEST, f"invalid JSON-RPC request: {exc}"))
        method = request.method
        params = request.params
        if request_id is None and method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                return success(request_id, initialize_result())
            if method == "notifications/initialized":
                return None
            if method == "ping":
                return success(request_id, {})
            if method == "tools/list":
                return success(
                    request_id, {"tools": [spec.model_dump() for spec, _, _ in self._tools.values()]}
                )
            if method == "tools/call":
                return success(request_id, self.call_tool_params(params))
        except JsonRpcError as exc:
            return failure(request_id, exc)
        except Exception as exc:  # pragma: no cover - defensive
            return failure(request_id, JsonRpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"))
        return failure(request_id, JsonRpcError(METHOD_NOT_FOUND, f"unknown method: {method}"))

    def call_tool_params(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or name not in self._tools:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool: {name}")
        _spec, args_model, handler = self._tools[name]
        try:
            args = args_model.model_validate(arguments)
        except Exception as exc:
            raise JsonRpcError(INVALID_PARAMS, f"invalid arguments for {name}: {exc}") from exc
        started = time.perf_counter()
        try:
            payload = handler(args)
            is_error = False
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}", "tool": name}
            is_error = True
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self.calls[name] = self.calls.get(name, 0) + 1
        result = text_content(payload)
        result["structuredContent"] = payload
        result["isError"] = is_error
        result["_meta"] = {"latency_ms": latency_ms, "server": "researchpilot-mcp", "tool": name}
        return result

    # -- tool handlers ------------------------------------------------------ #
    def _search_knowledge(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, SearchKnowledgeArgs)
        result = self.knowledge_base.search(
            args.query, top_k=args.top_k, strategy=args.strategy, filters=args.filters or None
        )
        return {
            "query": result.query,
            "rewritten_query": result.rewritten_query,
            "strategy": result.strategy,
            "items": [
                {
                    "source_id": hit.chunk.chunk_id,
                    "title": hit.chunk.title,
                    "content": hit.chunk.content,
                    "kind": "knowledge_base",
                    "doc_id": hit.chunk.doc_id,
                    "chunk_id": hit.chunk.chunk_id,
                    "score": round(hit.score, 4),
                    "metadata": {k: v for k, v in hit.chunk.metadata.items() if k != "clean_stats"},
                }
                for hit in result.hits
            ],
        }

    def _get_document(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, GetDocumentArgs)
        if args.chunk_id:
            chunk = self.knowledge_base.get_chunk(args.chunk_id)
            if chunk is None:
                return {"error": f"chunk not found: {args.chunk_id}", "items": []}
            content = chunk.content[: args.max_chars]
            return {
                "items": [
                    {
                        "source_id": chunk.chunk_id,
                        "title": chunk.title,
                        "content": content,
                        "kind": "knowledge_base",
                        "doc_id": chunk.doc_id,
                        "chunk_id": chunk.chunk_id,
                        "metadata": {k: v for k, v in chunk.metadata.items() if k != "clean_stats"},
                    }
                ]
            }
        document = self.knowledge_base.get_document(args.doc_id)
        if document is None:
            return {"error": f"document not found: {args.doc_id}", "items": []}
        return {
            "items": [
                {
                    "source_id": f"{document.doc_id}#full",
                    "title": document.title,
                    "content": document.content[: args.max_chars],
                    "kind": "document",
                    "doc_id": document.doc_id,
                    "metadata": {"source": document.source, **document.metadata},
                }
            ]
        }

    def _search_web(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, SearchWebArgs)
        results = self.web_backend.search(args.query, top_k=args.top_k)
        return {
            "query": args.query,
            "backend": getattr(self.web_backend, "name", "unknown"),
            "items": [
                {
                    "source_id": r.get("source_id"),
                    "title": r.get("title"),
                    "content": r.get("content") or r.get("snippet"),
                    "kind": "web",
                    "url": r.get("url"),
                    "score": r.get("score", 0.0),
                    "metadata": {
                        "date": r.get("date", ""),
                        "publisher": r.get("publisher", ""),
                        "synthetic": r.get("synthetic", True),
                    },
                }
                for r in results
            ],
        }

    def _research_context(self, args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ResearchContextArgs)
        knowledge = self._search_knowledge(SearchKnowledgeArgs(query=args.query, top_k=args.top_k))
        web = self._search_web(SearchWebArgs(query=args.query, top_k=max(args.top_k // 2, 2)))
        stats = self.knowledge_base.stats()
        items = list(knowledge["items"]) + list(web["items"])
        return {
            "server": "researchpilot-mcp",
            "subtask": args.subtask,
            "query": args.query,
            "items": items,
            "coverage": {
                "knowledge_base_documents": stats["documents"],
                "knowledge_base_chunks": stats["chunks"],
                "topics": stats["topics"][:12],
                "knowledge_hits": len(knowledge["items"]),
                "web_hits": len(web["items"]),
                "web_backend": web["backend"],
                "summary": truncate(" ".join(item.get("content", "") for item in items[:3]), 400),
            },
        }


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #
def configure_stdio_utf8() -> None:
    """Pin the stdio transport to UTF-8, independent of the OS locale.

    On Windows, a Python process whose stdio is a pipe uses the locale encoding
    (``cp936``/``gbk`` here) with ``surrogateescape``, which corrupts Chinese
    arguments on the way in and produces undecodable bytes on the way out. The MCP
    stdio wire format is UTF-8, so the server sets its own streams rather than
    relying on the launcher's ``PYTHONIOENCODING`` or ``-X utf8``.
    """
    for stream, errors in ((sys.stdin, "strict"), (sys.stdout, "strict")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # e.g. pytest's capture or an in-memory stream
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors=errors, newline="\n")
    stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
    if stderr_reconfigure is not None:
        # Logs stay on stderr and must never crash the protocol loop; replace any
        # character the local console cannot render.
        with contextlib.suppress(ValueError, OSError):
            stderr_reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")


def _iter_utf8_lines(stream: Any) -> Iterator[str]:
    """Yield request lines as ``str``, decoding the raw bytes as UTF-8.

    Reading the binary buffer skips the text layer, so the OS locale does not
    decide the encoding.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        # In-memory text stream (tests, embedded use).
        yield from stream
        return
    for raw in buffer:
        yield raw.decode("utf-8", errors="strict")


def _write_utf8_line(stream: Any, text: str) -> None:
    """Write one protocol line as UTF-8 with an explicit ``\\n`` terminator."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text + "\n")
        stream.flush()
        return
    buffer.write((text + "\n").encode("utf-8"))
    buffer.flush()


def serve_stdio(server: McpServer | None = None, *, stdin: Any = None, stdout: Any = None) -> None:
    """Newline-delimited JSON-RPC over stdio (the MCP stdio transport)."""
    configure_stdio_utf8()
    server = server or McpServer()
    stream_in = stdin or sys.stdin
    stream_out = stdout or sys.stdout
    # Startup notice goes to stderr: stdout must carry protocol messages only.
    # Emitted here (not in main) so every entry point - `python -m
    # researchpilot.mcp_server --stdio`, the console script and embedded use -
    # behaves identically.
    with contextlib.suppress(ValueError, OSError, AttributeError):
        sys.stderr.write(f"[researchpilot-mcp] stdio server ready (tools={sorted(server._tools)})\n")
        sys.stderr.flush()
    lines = _iter_utf8_lines(stream_in)
    try:
        while True:
            try:
                line = next(lines).strip()
            except StopIteration:
                break
            except UnicodeDecodeError as exc:
                decode_error: Any = failure(
                    None, JsonRpcError(PARSE_ERROR, f"request line is not valid UTF-8: {exc}")
                )
                _write_utf8_line(stream_out, json.dumps(decode_error, ensure_ascii=False))
                continue
            if not line:
                continue
            payload: Any
            try:
                message = json.loads(line)
                if isinstance(message, list):
                    responses = [server.handle(item) for item in message]
                    payload = [r for r in responses if r is not None]
                else:
                    payload = server.handle(message)
            except json.JSONDecodeError as exc:
                payload = failure(None, JsonRpcError(PARSE_ERROR, f"parse error: {exc}"))
            if payload is None:
                continue
            _write_utf8_line(stream_out, json.dumps(payload, ensure_ascii=False))
    finally:
        server.close()


def create_http_app(server: McpServer | None = None) -> Any:
    """Streamable-HTTP transport: ``POST /mcp`` with a JSON-RPC body."""
    mcp = server or McpServer()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            mcp.close()

    app = FastAPI(title="ResearchPilot MCP", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> Any:
        try:
            kb_stats = mcp.knowledge_base.stats()
        except PersistenceError:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "server": "researchpilot-mcp", "storage": "unavailable"},
            )
        return {
            "status": "degraded" if kb_stats.get("persistence_error") else "ok",
            "server": "researchpilot-mcp",
            "tools": sorted(mcp._tools),
            "calls": mcp.calls,
            "knowledge_base": kb_stats,
        }

    @app.post("/mcp")
    async def rpc(body: Any = Body(...)) -> JSONResponse:
        if isinstance(body, list):
            responses = [mcp.handle(item) for item in body]
            return JSONResponse([r for r in responses if r is not None])
        response = mcp.handle(body)
        if response is None:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "result": {}})
        return JSONResponse(response)

    app.state.mcp_server = mcp
    return app


def main() -> None:  # pragma: no cover - process entry point
    """Entry point: HTTP when RESEARCHPILOT_MCP_TRANSPORT=http, else stdio."""
    settings = get_settings()
    if settings.mcp_transport == "http":
        import uvicorn

        uvicorn.run(
            create_http_app(),
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level=settings.log_level.lower(),
        )
    else:
        serve_stdio(McpServer(settings))


if __name__ == "__main__":  # pragma: no cover
    main()
