"""Load raw documents from disk (markdown / text / json / jsonl)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from researchpilot.rag.models import Document
from researchpilot.utils import sha1_of, utc_now_iso

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".jsonl", ".csv"}


class DocumentLoader:
    """Turns files into :class:`Document` objects (with light front-matter support)."""

    def __init__(self, *, kind: str = "knowledge_base") -> None:
        self.kind = kind

    def load_path(self, path: str | Path) -> list[Document]:
        root = Path(path)
        if root.is_file():
            return self.load_file(root)
        if not root.exists():
            return []
        documents: list[Document] = []
        for file in sorted(root.rglob("*")):
            if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES:
                documents.extend(self.load_file(file, base=root))
        return documents

    def load_file(self, file: str | Path, *, base: Path | None = None) -> list[Document]:
        path = Path(file)
        suffix = path.suffix.lower()
        rel = str(path.relative_to(base)) if base else path.name
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".json":
            payload = json.loads(text)
            if isinstance(payload, list):
                return [self._from_mapping(item, rel, idx) for idx, item in enumerate(payload)]
            if isinstance(payload, dict):
                if "documents" in payload and isinstance(payload["documents"], list):
                    return [
                        self._from_mapping(item, rel, idx) for idx, item in enumerate(payload["documents"])
                    ]
                return [self._from_mapping(payload, rel, 0)]
            return []
        if suffix == ".jsonl":
            documents = []
            for idx, line in enumerate(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                documents.append(self._from_mapping(json.loads(line), rel, idx))
            return documents
        if suffix == ".csv":
            return self._from_csv(text, rel)
        return [self._from_markdown(text, rel, path)]

    # -- helpers ----------------------------------------------------------- #
    def _from_markdown(self, text: str, rel: str, path: Path) -> Document:
        metadata, body = _split_front_matter(text)
        metadata = _jsonify(metadata)
        title = str(metadata.get("title") or _first_heading(body) or path.stem)
        doc_id = str(metadata.get("id") or metadata.get("doc_id") or f"doc_{sha1_of(rel)}")
        return Document(
            doc_id=doc_id,
            title=title,
            source=str(metadata.get("source") or rel),
            kind=str(metadata.get("kind") or self.kind),  # type: ignore[arg-type]
            content=body,
            metadata=metadata,
            created_at=str(metadata.get("created_at") or utc_now_iso()),
        )

    def _from_mapping(self, item: Any, rel: str, idx: int) -> Document:
        if not isinstance(item, dict):
            item = {"content": str(item)}
        content = str(
            item.get("content") or item.get("text") or item.get("body") or item.get("snippet") or ""
        )
        title = str(item.get("title") or item.get("name") or f"{rel}#{idx}")
        source = str(item.get("source") or item.get("url") or rel)
        doc_id = str(item.get("doc_id") or item.get("id") or f"doc_{sha1_of(source + title)}")
        metadata = {
            k: v for k, v in item.items() if k not in {"content", "text", "body", "title", "doc_id", "id"}
        }
        metadata = _jsonify(metadata)
        return Document(
            doc_id=doc_id,
            title=title,
            source=source,
            kind=str(item.get("kind") or self.kind),  # type: ignore[arg-type]
            content=content,
            metadata=metadata,
            created_at=str(item.get("created_at") or utc_now_iso()),
        )

    def _from_csv(self, text: str, rel: str) -> list[Document]:
        import csv
        import io

        reader = csv.DictReader(io.StringIO(text))
        documents = []
        for idx, row in enumerate(reader):
            documents.append(self._from_mapping(dict(row), rel, idx))
        return documents


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        import yaml

        metadata = yaml.safe_load(parts[1]) or {}
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, parts[2].lstrip("\n")


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


def _jsonify(value: Any) -> Any:
    """Make YAML-parsed values JSON-serialisable (dates -> ISO strings)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonify(v) for v in value]
    return value
