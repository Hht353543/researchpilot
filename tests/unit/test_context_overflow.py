"""Context-overflow defences (spec §18.7): budgeted context + valid JSON prompts."""

from __future__ import annotations

import json
import re

from researchpilot.config import Settings
from researchpilot.llm import prompts
from researchpilot.rag.embeddings import HashEmbedder
from researchpilot.rag.models import Chunk
from researchpilot.rag.retriever import Retriever
from researchpilot.rag.vector_store import VectorStore


def _long_store(chunks: int = 6, content_chars: int = 1200) -> VectorStore:
    embedder = HashEmbedder(64)
    store = VectorStore(dimension=64)
    body = ("混合检索通过 RRF 融合语义与关键词排名。" * 60)[:content_chars]
    items = [
        Chunk(
            chunk_id=f"c{index}",
            doc_id=f"doc{index}",
            title=f"标题{index}",
            source=f"src{index}",
            content=f"{body} 片段编号 {index}",
            metadata={"topic": "rag"},
        )
        for index in range(chunks)
    ]
    store.add_chunks(
        [c.with_embedding(v) for c, v in zip(items, embedder.embed([c.content for c in items]), strict=True)]
    )
    return store


def test_retriever_truncates_context_to_the_budget() -> None:
    retriever = Retriever(_long_store(), HashEmbedder(64), settings=Settings(embedding_dim=64, top_k=6))
    result = retriever.retrieve("混合检索 RRF", top_k=6, max_context_chars=400)
    assert result.hits, "hits must still be returned"
    assert len(result.context) <= 400, "context must respect the configured budget"
    # The budget only limits the rendered context, not the retrieval itself.
    assert result.candidates >= 1


def test_knowledge_base_honours_max_context_chars_setting(tmp_path) -> None:
    """Regression: KnowledgeBase.search used to hardcode 12_000."""
    from researchpilot.rag.knowledge_base import KnowledgeBase

    settings = Settings(
        provider="mock",
        kb_path="tests/fixtures/kb",
        runs_path=str(tmp_path / "runs"),
        embedding_dim=64,
        top_k=4,
        max_context_chars=300,
    )
    kb = KnowledgeBase(settings)
    kb.ingest_path("tests/fixtures/kb")
    result = kb.search("工具注册表需要声明哪些字段？", top_k=4)
    assert result.context
    assert len(result.context) <= 300


def test_oversized_prompt_payloads_stay_valid_json() -> None:
    huge_evidence = [
        {
            "id": f"E{index}",
            "claim": "超长主张" * 200,
            "quote": "超长引用" * 200,
            "source_id": f"s{index}",
            "metadata": {"nested": {"deep": ["x" * 500] * 20}},
        }
        for index in range(40)
    ]
    prompt = prompts.writer_user(
        "目标" * 500,
        huge_evidence,
        {f"s{index}": {"kind": "web", "title": "t" * 800} for index in range(40)},
        {"checks": [{"evidence_id": "E1", "status": "supported"}]},
        {"issues": []},
        {"S1": "子问题"},
    )
    block = re.search(r'<untrusted source="evidence">\n(.*?)\n</untrusted>', prompt, re.S).group(1)
    parsed = json.loads(block)  # must remain parseable even after shrinking
    assert isinstance(parsed, list) and parsed
    assert len(block) <= 6100, "payload must respect the untrusted-block budget"


def test_short_term_memory_bounds_context_growth() -> None:
    from researchpilot.memory.short_term import ShortTermMemory

    memory = ShortTermMemory(max_tokens=120, max_items=100)
    for index in range(50):
        memory.add("tool", f"observation {index} " + "x" * 200)
    assert memory.total_tokens() <= 120
    assert len(memory.items) < 50
