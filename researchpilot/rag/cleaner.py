"""Text cleaning: boilerplate removal, whitespace normalisation, de-duplication."""

from __future__ import annotations

import re
import unicodedata

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOILERPLATE_RE = re.compile(
    r"^\s*(版权所有|copyright|all rights reserved|关注公众号|扫码关注|点击关注|订阅|"
    r"阅读全文|免责声明|cookie|privacy policy|advertisement)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_HTML_TAG_RE = re.compile(r"<(?!/?(code|pre)\b)[^>]{1,80}>")


class TextCleaner:
    """Deterministic cleaner; returns text plus a small statistics dict."""

    def __init__(self, *, strip_html: bool = True, drop_boilerplate: bool = True) -> None:
        self.strip_html = strip_html
        self.drop_boilerplate = drop_boilerplate

    def clean(self, text: str) -> tuple[str, dict[str, float]]:
        original = text or ""
        cleaned = unicodedata.normalize("NFKC", original)
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        if self.strip_html:
            cleaned = _HTML_TAG_RE.sub(" ", cleaned)
        cleaned = _CONTROL_RE.sub("", cleaned)
        if self.drop_boilerplate:
            cleaned = _BOILERPLATE_RE.sub("", cleaned)
        lines: list[str] = []
        seen: set[str] = set()
        removed_duplicates = 0
        for line in cleaned.split("\n"):
            stripped = line.rstrip()
            key = stripped.strip()
            if key and len(key) > 24:
                if key in seen:
                    removed_duplicates += 1
                    continue
                seen.add(key)
            lines.append(stripped)
        cleaned = "\n".join(lines)
        cleaned = _MULTI_BLANK_RE.sub("\n\n", cleaned).strip()
        stats = {
            "chars_in": float(len(original)),
            "chars_out": float(len(cleaned)),
            "compression": round(1 - len(cleaned) / max(len(original), 1), 4),
            "duplicate_lines_removed": float(removed_duplicates),
        }
        return cleaned, stats
