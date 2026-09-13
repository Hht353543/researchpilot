"""Scripted OpenAI-compatible HTTP endpoint for integration tests.

It implements ``POST /v1/chat/completions`` and ``POST /v1/embeddings`` so the
repository can verify the *real* HTTP provider path (request payloads, auth,
structured-output parsing, retries, timeouts, usage accounting, embeddings)
without depending on an external vendor or a private API key.

The chat endpoint reads the JSON payloads that our own prompts embed in
``<untrusted>`` blocks, so the reports it produces are grounded in the real
retrieved observations rather than canned text.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException

_EVIDENCE_ID_RE = re.compile(r'"id"\s*:\s*"(E\d+)"')
_SOURCE_ID_RE = re.compile(r"\[([A-Za-z0-9_\-:.]+)#?[^\]]*\]")


class StubState:
    """Mutable counters so tests can assert retries / auth / timeout behaviour."""

    def __init__(
        self,
        *,
        api_key: str | None = "test-key",
        fail_first: int = 0,
        latency_s: float = 0.0,
        embed_dim: int = 64,
        model: str = "gpt-4o-mini",
    ) -> None:
        self.api_key = api_key
        self.fail_first = fail_first
        self.latency_s = latency_s
        self.embed_dim = embed_dim
        self.model = model
        self.chat_requests = 0
        self.embedding_requests = 0
        self.last_schemas: list[str] = []
        self.lock = threading.Lock()

    def begin_chat(self) -> int:
        with self.lock:
            self.chat_requests += 1
            return self.chat_requests


def _embed(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        vector[int.from_bytes(digest[:4], "big") % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


def _block(prompt: str, label: str) -> str | None:
    match = re.search(rf'<untrusted source="{label}">\n(.*?)\n</untrusted>', prompt, re.S)
    return match.group(1) if match else None


def _plan(prompt: str) -> dict[str, Any]:
    question = _block(prompt, "user-question") or "未命名研究任务"
    return {
        "objective": question.strip(),
        "subtasks": [
            {
                "id": "S1",
                "question": f"{question.strip()} —— 检索内部知识库与外部资料",
                "intent": "knowledge_search",
                "tools": ["knowledge_search", "web_search"],
                "expected_output": "可引用的证据条目",
                "priority": 5,
                "depends_on": [],
            },
            {
                "id": "S2",
                "question": f"{question.strip()} —— 交叉验证并形成结论",
                "intent": "synthesis",
                "tools": [],
                "expected_output": "经过验证的结论",
                "priority": 5,
                "depends_on": ["S1"],
            },
        ],
        "requires_knowledge_base": True,
        "requires_web": True,
        "requires_mcp": False,
        "success_criteria": ["结论绑定引用"],
        "max_iterations": 1,
        "rationale": "stub planner for provider-path integration tests",
    }


def _evidence(prompt: str) -> dict[str, Any]:
    """Extract quotable evidence from the observation blocks in the prompt."""
    text = _block(prompt, "knowledge_search") or ""
    text += "\n" + (_block(prompt, "web_search") or "")
    if not text.strip():
        text = prompt
    items: list[dict[str, Any]] = []
    parts = re.split(r"(?=\[[A-Za-z0-9_\-:.]+#?[^\]]*\])", text)
    for part in parts:
        match = _SOURCE_ID_RE.match(part.strip())
        if not match:
            continue
        source_id = part.strip().split("]", 1)[0].lstrip("[")
        body = part.split("]", 1)[1].strip() if "]" in part else ""
        sentence = next((s.strip() for s in re.split(r"[。；!\n]", body) if len(s.strip()) >= 16), "")
        if not sentence:
            continue
        items.append(
            {
                "id": f"E{len(items) + 1}",
                "claim": sentence[:120],
                "quote": sentence[:200],
                "source_id": source_id,
                "tool": "knowledge_search",
                "confidence": 0.6,
                "relevance": 0.6,
            }
        )
        if len(items) >= 4:
            break
    return {"evidence": items, "sources": [], "gaps": [] if items else ["no observation found"]}


def _verification(prompt: str) -> dict[str, Any]:
    return {
        "checks": [],
        "unsupported_claims": [],
        "flagged_sources": [],
        "source_quality": {},
        "coverage_score": 0.0,
        "sufficient": True,
        "missing_topics": [],
        "overall_score": 0.0,
    }


def _critique(prompt: str) -> dict[str, Any]:
    return {
        "issues": [],
        "needs_more_research": False,
        "follow_up_queries": [],
        "coverage": {},
        "overall_assessment": "stub critique",
    }


def _report(prompt: str) -> dict[str, Any]:
    raw = _block(prompt, "evidence") or "[]"
    try:
        evidence = json.loads(raw)
    except Exception:
        evidence = []
    conclusions = [
        {
            "statement": str(item.get("claim", ""))[:160],
            "evidence_ids": [str(item.get("id"))],
            "confidence": 0.7,
        }
        for item in evidence[:4]
        if item.get("claim")
    ]
    objective = _block(prompt, "user-question") or "研究任务"
    return {
        "title": f"研究报告：{objective.strip()[:60]}",
        "executive_summary": f"基于 {len(evidence)} 条证据生成的结论摘要。",
        "conclusions": conclusions,
        "sections": [
            {
                "heading": "证据概述",
                "body": "\n".join(f"- {item.get('claim')} [{item.get('id')}]" for item in evidence[:6])
                or "- 无可用证据。",
                "evidence_ids": [str(item.get("id")) for item in evidence[:6]],
            }
        ],
        "recommendations": ["继续补充一手资料。"],
        "limitations": ["该报告由本地 OpenAI 兼容端点生成，用于验证 provider 链路。"],
    }


_HANDLERS = {
    "ResearchPlan": _plan,
    "EvidenceBundle": _evidence,
    "VerificationReport": _verification,
    "CritiqueReport": _critique,
    "FinalReport": _report,
}


def create_stub_app(state: StubState) -> FastAPI:
    app = FastAPI(title="OpenAI-compatible stub")
    app.state.stub = state

    @app.post("/v1/chat/completions")
    def chat(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> Any:
        if state.api_key and authorization != f"Bearer {state.api_key}":
            raise HTTPException(status_code=401, detail="invalid api key")
        index = state.begin_chat()
        if state.latency_s:
            time.sleep(state.latency_s)
        if index <= state.fail_first:
            raise HTTPException(status_code=500, detail="injected upstream failure")

        schema_name = (payload.get("response_format") or {}).get("json_schema", {}).get("name", "")
        state.last_schemas.append(schema_name)
        prompt = "\n".join(str(m.get("content", "")) for m in payload.get("messages", []))
        handler = _HANDLERS.get(schema_name)
        if handler is None:
            raise HTTPException(status_code=400, detail=f"unsupported schema: {schema_name}")
        content = json.dumps(handler(prompt), ensure_ascii=False)
        prompt_tokens = max(len(prompt) // 4, 1)
        completion_tokens = max(len(content) // 4, 1)
        return {
            "id": f"chatcmpl-{index}",
            "model": payload.get("model") or state.model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @app.post("/v1/embeddings")
    def embeddings(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> Any:
        if state.api_key and authorization != f"Bearer {state.api_key}":
            raise HTTPException(status_code=401, detail="invalid api key")
        state.embedding_requests += 1
        inputs = payload.get("input")
        texts = [inputs] if isinstance(inputs, str) else list(inputs or [])
        return {
            "object": "list",
            "model": payload.get("model") or "stub-embedding",
            "data": [
                {"object": "embedding", "index": idx, "embedding": _embed(text, state.embed_dim)}
                for idx, text in enumerate(texts)
            ],
        }

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def run_stub_server(**kwargs: Any) -> Iterator[tuple[str, StubState]]:
    """Start the stub on a free localhost port; yields ``(base_url, state)``."""
    state = StubState(**kwargs)
    app = create_stub_app(state)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="openai-stub", daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - startup failure
        raise RuntimeError("stub server failed to start")
    try:
        yield f"http://127.0.0.1:{port}/v1", state
    finally:
        server.should_exit = True
        thread.join(timeout=10)
