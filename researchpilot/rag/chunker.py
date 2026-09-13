"""Heading-aware chunker with overlap (documents stay traceable to their section)."""

from __future__ import annotations

import re
from typing import Any

from researchpilot.rag.cleaner import TextCleaner
from researchpilot.rag.models import Chunk, Document
from researchpilot.utils import estimate_tokens, sha1_of

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_FENCE_RE = re.compile(r"^```")
_SENTENCE_END_RE = re.compile(r"[。！？；!?;\n]")


class MarkdownChunker:
    """Splits a document into section-aware chunks of roughly ``target_chars``."""

    def __init__(
        self,
        *,
        target_chars: int = 700,
        max_chars: int = 1000,
        overlap_chars: int = 120,
        min_chars: int = 60,
        cleaner: TextCleaner | None = None,
    ) -> None:
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.min_chars = min_chars
        self.cleaner = cleaner or TextCleaner()

    def chunk_document(self, document: Document) -> list[Chunk]:
        cleaned, clean_stats = self.cleaner.clean(document.content)
        if not cleaned:
            return []
        blocks = self._blocks(cleaned)
        path: list[str] = [document.title]
        chunks: list[Chunk] = []
        buffer: list[str] = []
        buffer_len = 0
        leftover = ""
        carry = ""
        position = 0

        def heading_ctx() -> str:
            return " > ".join(item for item in path if item)

        def emit() -> None:
            nonlocal buffer, buffer_len, leftover, carry, position
            parts = ([leftover] if leftover else []) + buffer
            body = "\n\n".join(part for part in parts if part).strip()
            buffer, buffer_len, leftover = [], 0, ""
            if not body:
                return
            context = heading_ctx()
            content = f"{context}\n\n{body}" if context else body
            if carry:
                content = f"{context}\n\n{carry}\n\n{body}" if context else f"{carry}\n\n{body}"
            content = content.strip()
            if len(content) > self.max_chars:
                cut = self._sentence_cut(content)
                leftover = content[cut:].strip()
                content = content[:cut].strip()
            if not content:
                return
            if len(content) < self.min_chars and chunks and not leftover:
                previous = chunks[-1]
                merged = f"{previous.content}\n\n{content}".strip()
                chunks[-1] = previous.model_copy(
                    update={
                        "content": merged,
                        "metadata": {**previous.metadata, "chars": len(merged)},
                    }
                )
                return
            metadata: dict[str, Any] = {
                "section": context or document.title,
                "position": position,
                "chars": len(content),
                "tokens": estimate_tokens(content),
                "clean_stats": clean_stats,
            }
            metadata.update({k: v for k, v in document.metadata.items() if k not in metadata})
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}#c{position:03d}",
                    doc_id=document.doc_id,
                    title=document.title,
                    source=document.source,
                    kind=document.kind,
                    content=content,
                    metadata=metadata,
                    created_at=document.created_at,
                )
            )
            carry = self._tail_overlap(content)
            position += 1

        for kind, text in blocks:
            if kind == "heading":
                if buffer_len >= self.min_chars:
                    emit()
                title = text.strip("# ").strip()
                level = len(text) - len(text.lstrip("#"))
                if title:
                    del path[max(level - 1, 0) :]
                    path.append(title)
                continue
            if buffer_len + len(text) > self.max_chars and buffer:
                emit()
            buffer.append(text)
            buffer_len += len(text)
            if buffer_len >= self.target_chars:
                emit()
        emit()
        if not chunks:
            digest = sha1_of(cleaned)
            chunks.append(
                Chunk(
                    chunk_id=f"{document.doc_id}#c000",
                    doc_id=document.doc_id,
                    title=document.title,
                    source=document.source,
                    kind=document.kind,
                    content=cleaned,
                    metadata={"section": document.title, "position": 0, "hash": digest},
                    created_at=document.created_at,
                )
            )
        return chunks

    def _sentence_cut(self, content: str) -> int:
        """Cut ``content`` at a sentence boundary within [target, max] chars."""
        ceiling = min(self.max_chars, len(content))
        floor = min(self.target_chars, ceiling)
        candidates = [match.end() for match in _SENTENCE_END_RE.finditer(content[:ceiling])]
        if not candidates:
            return ceiling
        best = max(candidates)
        return best if best >= floor else ceiling

    def _tail_overlap(self, content: str) -> str:
        """Last complete sentences within the overlap window (never start mid-sentence)."""
        if self.overlap_chars <= 0:
            return ""
        window = content[-self.overlap_chars :]
        match = _SENTENCE_END_RE.search(window)
        if not match:
            return ""
        tail = window[match.end() :].strip()
        return tail if len(tail) >= self.min_chars else ""

    @staticmethod
    def _blocks(text: str) -> list[tuple[str, str]]:
        """Group lines into blocks: headings, code fences, paragraphs, list runs."""
        blocks: list[tuple[str, str]] = []
        current: list[str] = []
        in_code = False

        def flush(kind: str = "paragraph") -> None:
            nonlocal current
            body = "\n".join(current).strip()
            if body:
                blocks.append((kind, body))
            current = []

        for line in text.split("\n"):
            if _CODE_FENCE_RE.match(line.strip()):
                if in_code:
                    current.append(line)
                    flush("code")
                    in_code = False
                else:
                    flush()
                    current.append(line)
                    in_code = True
                continue
            if in_code:
                current.append(line)
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                flush()
                blocks.append(("heading", line))
                continue
            if not line.strip():
                flush()
                continue
            current.append(line)
        if in_code:
            flush("code")
        flush()
        return blocks
