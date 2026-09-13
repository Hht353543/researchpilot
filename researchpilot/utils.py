"""Small shared helpers: token estimation, JSON handling, text normalisation."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_WS_RE = re.compile(r"[ \t\u3000]+")


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate.

    CJK characters are ~1 token each, latin words ~1.3 tokens. Used for
    budgeting/telemetry only; real providers report exact usage.
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    other = max(len(text) - cjk - sum(len(w) for w in _WORD_RE.findall(text)), 0)
    return int(cjk + words * 1.3 + other / 6) + 1


def sha1_of(text: str, *, length: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace - used for de-duplication and matching."""
    return _WS_RE.sub(" ", text.replace("\r\n", "\n")).strip().lower()


def truncate(text: str, limit: int, *, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - len(suffix), 0)] + suffix


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def project_root() -> Path:
    """Repository root, resolved from this file (independent of the process cwd)."""
    return Path(__file__).resolve().parents[1]


def resolve_input_path(value: str | Path, *, root: Path | None = None) -> Path:
    """Resolve an *input* path relative to cwd first, then to the project root.

    Entry points (CLI, API, scripts) may run from any working directory. Silently
    reading an empty knowledge base / web corpus / pricing table because of cwd
    drift is a real bug, so inputs are looked up in both locations.
    """
    path = Path(value)
    if path.is_absolute() or path.exists():
        return path
    candidate = (root or project_root()) / path
    return candidate if candidate.exists() else path


def parse_iso(value: str) -> float | None:
    """Parse an ISO-8601 UTC stamp into epoch seconds (None when unparseable)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def new_id(prefix: str) -> str:
    return f"{prefix}_{hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:12]}"


@contextmanager
def timed() -> Iterator[dict[str, float]]:
    """``with timed() as t: ...`` -> ``t['ms']`` holds the elapsed milliseconds."""
    box: dict[str, float] = {}
    start = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = round((time.perf_counter() - start) * 1000, 3)


def load_pricing_file(path: str | Path) -> dict[str, dict[str, float]]:
    """Read a YAML/JSON pricing table; missing or broken files are ignored."""
    file = Path(path)
    if not file.exists():
        return {}
    try:
        raw = file.read_text(encoding="utf-8")
        if file.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(raw) or {}
        else:
            data = json.loads(raw)
    except Exception:  # pragma: no cover - defensive
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for model, prices in data.items():
        if isinstance(prices, dict) and "input" in prices and "output" in prices:
            out[str(model)] = {"input": float(prices["input"]), "output": float(prices["output"])}
    return out


def extract_json_block(text: str) -> Any:
    """Best-effort JSON extraction from an LLM answer.

    Handles ```json fences, leading prose and trailing commentary. Falls back to
    ``json_repair`` when available, then to a brace-balancing scan.
    """
    if not text:
        raise ValueError("empty LLM response")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    for candidate in (cleaned, _brace_span(cleaned)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:  # pragma: no cover - optional dependency
                from json_repair import repair_json

                repaired = repair_json(candidate)
                if repaired:
                    return json.loads(repaired)
            except Exception:
                pass
    raise ValueError("no JSON object found in LLM response")


def _brace_span(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return text[start:]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    size = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(size))
    na = sum(x * x for x in a[:size]) ** 0.5
    nb = sum(x * x for x in b[:size]) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def token_set(text: str) -> set[str]:
    """Tokenise for lexical overlap scoring (latin words + CJK bi-grams).

    CJK bi-grams are only formed from *adjacent* characters inside the same run, so
    punctuation never produces meaningless tokens such as ``与关``.
    """
    lowered = normalize_text(text)
    tokens = {w.lower() for w in _WORD_RE.findall(lowered)}
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.update(run)
        tokens.update(a + b for a, b in itertools.pairwise(run))
    return {t for t in tokens if t}


_STOPWORDS: set[str] = {
    # CJK function words / very common bigrams
    *[
        "的了是在和与或及也都就而而且并等被把对从到于其之这那有没不是为以为所以如果因为可以需要应该一个一种一些这个那个我们你们他们它他她"
    ],
    "什么",
    "哪些",
    "怎么",
    "如何",
    "分别",
    "以及",
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "and",
    "or",
    "is",
    "are",
    "for",
    "with",
    "what",
    "which",
    "how",
    "does",
    "do",
}


def content_tokens(text: str) -> set[str]:
    """Word-level, stopword-free tokens used for topical relevance scoring.

    Uses jieba when available (real Chinese words such as ``拓扑`` / ``编排``) and falls
    back to the character bigram tokeniser otherwise.
    """
    lowered = normalize_text(text)
    tokens: set[str] = set()
    try:  # pragma: no cover - optional dependency
        import jieba

        tokens.update(w.strip() for w in jieba.lcut(lowered) if w.strip())
    except Exception:
        pass
    tokens.update(_WORD_RE.findall(lowered))
    filtered = {t for t in tokens if t not in _STOPWORDS and len(t) > 1}
    filtered = {t for t in filtered if not _PUNCT_RE.search(t)}
    return filtered or token_set(text)


_PUNCT_RE = re.compile(r"^[\W_]+$", flags=re.UNICODE)


def _is_ascii_word(token: str) -> bool:
    return token.isascii() and token.isalnum() and len(token) >= 2


def content_overlap(reference: str, candidate: str) -> float:
    """Fraction of the reference's content tokens that appear in the candidate."""
    ref = content_tokens(reference)
    if not ref:
        return 0.0
    return len(ref & content_tokens(candidate)) / len(ref)


def jaccard(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap_ratio(reference: str, candidate: str) -> float:
    """Fraction of ``reference`` tokens that also appear in ``candidate``."""
    ref = token_set(reference)
    if not ref:
        return 0.0
    return len(ref & token_set(candidate)) / len(ref)
