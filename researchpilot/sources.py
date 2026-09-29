"""Source registry: evidence points at sources through stable ids."""

from __future__ import annotations

from typing import Any

from researchpilot.schemas import SourceKind, SourceRef
from researchpilot.utils import new_id, utc_now_iso

_TOOL_KIND: dict[str, SourceKind] = {
    "knowledge_search": "knowledge_base",
    "document_reader": "document",
    "web_search": "web",
    "mcp_research_context": "mcp",
    "metadata": "knowledge_base",
    "calculator": "computation",
}


class SourceRegistry:
    """Collects every source touched during a research task."""

    def __init__(self) -> None:
        self._sources: dict[str, SourceRef] = {}
        self._by_doc: dict[str, str] = {}
        self._content: dict[str, str] = {}

    def register_item(
        self,
        item: dict[str, Any],
        *,
        tool: str,
        kind: SourceKind | None = None,
        retrieved_at: str | None = None,
    ) -> SourceRef:
        source_id = str(item.get("source_id") or item.get("chunk_id") or item.get("doc_id") or new_id("src"))
        if source_id in self._sources:
            return self._sources[source_id]
        metadata = item.get("metadata") or {}
        resolved_kind: SourceKind = kind or _TOOL_KIND.get(tool, "web")
        if item.get("kind") in {"knowledge_base", "document", "web", "mcp", "computation"}:
            resolved_kind = item["kind"]
        ref = SourceRef(
            id=source_id,
            kind=resolved_kind,
            title=str(item.get("title") or metadata.get("title") or source_id),
            locator=str(item.get("source") or item.get("url") or item.get("doc_id") or ""),
            doc_id=str(item.get("doc_id") or ""),
            chunk_id=str(item.get("chunk_id") or ""),
            url=str(item.get("url") or metadata.get("url") or ""),
            retrieved_at=retrieved_at or utc_now_iso(),
            score=float(item.get("score") or 0.0),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
        self._sources[source_id] = ref
        content = item.get("content")
        if isinstance(content, str) and content:
            self._content[source_id] = content[:20_000]
        if ref.doc_id:
            self._by_doc.setdefault(ref.doc_id, source_id)
        return ref

    def content_of(self, source_id: str) -> str:
        """Raw text of a source as it was returned by the tool (for grounding checks)."""
        return self._content.get(source_id, "")

    def has(self, source_id: str) -> bool:
        return source_id in self._sources

    def source_for_doc(self, doc_id: str) -> SourceRef | None:
        """First source registered for a document (used by document_reader)."""
        source_id = self._by_doc.get(doc_id)
        return self._sources.get(source_id) if source_id else None

    def register_tool_result(self, result: Any) -> list[SourceRef]:
        registered: list[SourceRef] = []
        for item in getattr(result, "items", []) or []:
            payload = item if isinstance(item, dict) else item.model_dump()
            registered.append(self.register_item(payload, tool=getattr(result, "tool", "")))
        if not registered:
            data = getattr(result, "data", {}) or {}
            document = data.get("document")
            if isinstance(document, dict):
                registered.append(
                    self.register_item(document, tool=getattr(result, "tool", ""), kind="document")
                )
        return registered

    def get(self, source_id: str) -> SourceRef | None:
        return self._sources.get(source_id)

    def all(self) -> list[SourceRef]:
        return list(self._sources.values())

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {sid: ref.model_dump() for sid, ref in self._sources.items()}

    def doc_ids(self) -> list[str]:
        return sorted({ref.doc_id for ref in self._sources.values() if ref.doc_id})

    def urls(self) -> list[str]:
        return sorted({ref.url for ref in self._sources.values() if ref.url})
