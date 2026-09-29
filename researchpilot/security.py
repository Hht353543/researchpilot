"""Prompt-injection / tool-abuse guards used across tools and agents."""

from __future__ import annotations

import re

from researchpilot.utils import truncate

INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)", "ignore-previous"),
    (r"disregard\s+(the\s+)?(system|previous)\s+(prompt|instructions)", "disregard-system"),
    (r"忽略(以上|之前|前面|上述)?.{0,6}(指令|指示|规则|提示词)", "ignore-zh"),
    (r"(reveal|print|show|leak)\s+(your\s+)?(system\s+)?(prompt|instructions|rules)", "prompt-leak"),
    (r"(显示|输出|打印|泄露|告诉我).{0,6}(系统提示|提示词|系统指令|你的指令)", "prompt-leak-zh"),
    (r"you\s+are\s+now\s+(a|an|the)\s+", "role-switch"),
    (
        r"(你现在是|从现在开始你是|扮演|你是).{0,12}"
        r"(管理员|root|开发者模式|开发者|超级用户|越狱)",
        "role-switch-zh",
    ),
    (r"(developer|debug|god|dan)\s+mode", "dev-mode"),
    (
        r"(泄露|输出|导出|打印|提供|告诉我|给我).{0,10}"
        r"(api[_\s-]?key|密钥|凭据|token|密码|secret)",
        "secret-leak-zh",
    ),
    (r"<\s*/?\s*(system|assistant|tool)\s*>", "fake-role-tag"),
    (r"(api[_\s-]?key|token|password|secret)\s*[:=]", "secret-probe"),
    (r"(rm\s+-rf|del\s+/[sq]|format\s+c:|shutdown\s+/s)", "destructive-command"),
    (r"(delete|drop)\s+(all|the)\s+(files|documents|database|table)", "destructive-intent"),
)

_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in INJECTION_PATTERNS)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s,;]+")
_UNIX_PATH = re.compile(r"(?<![:\w])/(?:[^/\s]+/)+[^\s,;]*")
_NAMED_SECRET = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+")
_TOKEN_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def detect_injection(text: str) -> list[str]:
    """Return the labels of every injection/abuse pattern found in ``text``."""
    if not text:
        return []
    hits: list[str] = []
    for pattern, label in _COMPILED:
        if pattern.search(text):
            hits.append(label)
    return hits


def risk_score(text: str) -> float:
    """0.0 (clean) .. 1.0 (multiple strong signals)."""
    hits = detect_injection(text)
    return round(min(len(set(hits)) * 0.25, 1.0), 3)


def wrap_untrusted(label: str, text: str, *, max_chars: int = 6_000) -> str:
    """Neutralise tag-breaking attempts before embedding content in a prompt."""
    safe = truncate(text, max_chars).replace("</untrusted>", "<\\/untrusted>")
    return f'<untrusted source="{label}">\n{safe}\n</untrusted>'


def safe_diagnostic(message: str, *, secrets: tuple[str | None, ...] = ()) -> str:
    """Keep actionable error context while removing credentials and local paths."""
    safe = str(message)
    for secret in secrets:
        if secret and len(secret) >= 4:
            safe = safe.replace(secret, "[redacted]")
    safe = _NAMED_SECRET.sub(r"\1=[redacted]", safe)
    safe = _TOKEN_SECRET.sub("[redacted]", safe)
    safe = _WINDOWS_PATH.sub("[path]", safe)
    return _UNIX_PATH.sub("[path]", safe)
