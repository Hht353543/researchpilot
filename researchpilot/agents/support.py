"""Conservative source checks. Lexical similarity is never proof of entailment."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Literal

from researchpilot.schemas import CitationCheck
from researchpilot.tools.implementations.calculator import _evaluate
from researchpilot.utils import overlap_ratio

_CITATIONS = re.compile(r"[\[【［]\s*E\d+(?:\s*[,、，]\s*E\d+)*\s*[\]】］]", re.I)
_CLAUSES = re.compile(r"[。；;，,!?！？\n]+|(?<!\d)\.(?!\d)")
_SENTENCES = re.compile(r"[。；;!?！？\n]+|(?<!\d)\.(?!\d)")
_NEGATIVE = re.compile(
    r"不(?:支持|能|可|需要|会|是|允许|提供|得|具备|包含)|无(?:须|需|法|效)|"
    r"未(?:支持|提供|启用|达到|能|完成|超过)|禁止|拒绝|不得|\b(?:not|never|no|cannot|without)\b",
    re.I,
)
_NUMBERS = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|万元|千元|亿元|美元|毫秒|万|千|亿|元|秒|ms|GB|MB|年|月|日)?", re.I
)
_IDENTIFIERS = re.compile(r"\b(?:ISO\s*\d+(?::\d+)?|import_[\da-f]+(?:#c\d+)?|E\d+)\b", re.I)


def calculation_numbers(claim: str, quote: str) -> set[str]:
    """Allow new numbers only in reproducible arithmetic using cited inputs.

    This validates arithmetic, not its meaning; the semantic review still checks
    the subject, units and choice of denominator.
    """
    allowed, valid = _calculations(claim, quote)
    return allowed if valid else set()


def _calculations(claim: str, quote: str) -> tuple[set[str], bool]:
    normalized = unicodedata.normalize("NFKC", claim).replace("×", "*").replace("÷", "/").replace("−", "-")
    normalized = _NUMBERS.sub(
        lambda match: re.findall(r"\d+(?:\.\d+)?", match[0])[0] + ("%" if "%" in match[0] else ""),
        normalized,
    )
    # A common percentage formula writes '* 100% = 100%' for conversion
    # to the displayed percentage; evaluate its numeric percentage value.
    normalized = re.sub(r"\*\s*100%\s*=(?=\s*\d+(?:\.\d+)?%)", "* 100 =", normalized)
    inputs = {float(number) for number in re.findall(r"\d+(?:\.\d+)?", quote)}
    allowed: set[str] = set()
    for match in re.finditer(r"([\d.()\s+*/-]+[+*/-][\d.()\s+*/-]+)\s*=\s*(\d+(?:\.\d+)?)(%)?", normalized):
        expression, result, percent = match.groups()
        operands = {float(number) for number in re.findall(r"\d+(?:\.\d+)?", expression)}
        conversion = (
            {100.0} if percent and "/" in expression and re.search(r"\*\s*100\s*$", expression) else set()
        )
        if not operands - conversion <= inputs or len(expression) > 300:
            return set(), False
        try:
            value = float(_evaluate(expression.strip()))
        except (ValueError, SyntaxError, ArithmeticError):
            return set(), False
        if math.isfinite(value) and math.isclose(value, float(result), rel_tol=1e-6, abs_tol=1e-6):
            allowed.add(result + ("%" if percent else ""))
            allowed.update(re.findall(r"\d+(?:\.\d+)?", expression))
            for token in _NUMBERS.findall(claim):
                compact = re.sub(r"\s+", "", token).lower()
                unit = re.sub(r"^\d+(?:\.\d+)?", "", compact)
                number = re.findall(r"\d+(?:\.\d+)?", compact)[0]
                if float(number) == float(result) and unit and unit in quote.lower():
                    allowed.add(compact)
            if conversion:
                allowed.add("100")
        else:
            return set(), False
    return allowed, True


def clean_statement(text: str) -> str:
    return _CITATIONS.sub("", text).strip().strip("-•* \t")


def _parts(text: str, *, sentences: bool = False) -> list[str]:
    return [
        re.sub(r"\s+", " ", part).strip(" -*•")
        for part in (_SENTENCES if sentences else _CLAUSES).split(
            unicodedata.normalize("NFKC", clean_statement(text))
        )
        if part.strip()
    ]


def direct_support(claim: str, quote: str) -> bool:
    """Only whole sentences qualify; commas may carry essential conditions."""
    claims, quotes = _parts(claim, sentences=True), _parts(quote, sentences=True)
    return bool(claims) and all(part in quotes for part in claims)


def quote_grounded(quote: str, source: str) -> bool:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", quote))
    content = re.sub(r"\s+", "", unicodedata.normalize("NFKC", source))
    if not compact:
        return False
    # A substring immediately following negation drops essential source context.
    for match in re.finditer(re.escape(compact), content):
        prefix = content[max(0, match.start() - 12) : match.start()]
        if not re.search(r"(?:不|未|无|禁止|拒绝|不再|并非|not|never|cannot)$", prefix, re.I):
            return True
    return False


def contradiction(claim: str, quote: str, *, context: str | None = None) -> str:
    derived_numbers, arithmetic_valid = _calculations(claim, quote)
    if not arithmetic_valid:
        return "arithmetic is incorrect or uses inputs absent from the cited text"
    # Standard names and source IDs are subjects/identifiers, not measurements.
    # Their meaning remains part of the semantic review of the original text.
    numbers = {re.sub(r"\s+", "", number).lower() for number in _NUMBERS.findall(_IDENTIFIERS.sub("", claim))}
    quoted_numbers = {
        re.sub(r"\s+", "", number).lower() for number in _NUMBERS.findall(_IDENTIFIERS.sub("", quote))
    }
    # Omitting a repeated unit (e.g. '2025 to 2026 年') is different from
    # changing one. Explicit units still have to match exactly.
    quoted_values = {float(re.findall(r"\d+(?:\.\d+)?", number)[0]) for number in quoted_numbers}
    bare_values = {
        number
        for number in numbers
        if re.fullmatch(r"\d+(?:\.\d+)?", number) and float(number) in quoted_values
    }
    if numbers - quoted_numbers - bare_values - derived_numbers:
        return "claim changes a number, unit or date in the quote"
    context = context or quote
    for part in _parts(claim):
        if re.search(r"(?:未|没有|未曾|缺少|缺失).{0,12}(?:提供|说明|标明|注明)?.{0,12}单位", part):
            subjects = [
                word
                for word in ("收入", "金额", "价格", "费用", "存储", "上传", "响应", "时长")
                if word in part
            ]
            for source_part in _parts(context):
                if subjects and not any(subject in source_part for subject in subjects):
                    continue
                if re.search(
                    r"\d+(?:\.\d+)?\s*(?:万元|千元|亿元|美元|毫秒|元|秒|ms|GB|MB|%)", source_part, re.I
                ):
                    return "claim says units are missing although the source supplies them"
        if (
            re.search(r"矛盾|冲突|不一致", part)
            and re.search(r"上线|发布|成立|投产", claim)
            and re.search(r"收入|营收|销售额", claim)
            and not re.search(r"矛盾|冲突|不一致|不得|不能|不可能", context)
        ):
            return "revenue and a launch date alone do not establish a temporal contradiction"
    for part in _parts(claim):
        candidates = _parts(quote)
        if not candidates:
            continue
        closest = max(candidates, key=lambda candidate: overlap_ratio(part, candidate))
        epistemic = re.search(r"无法(?:确认|确定|判断)|不能(?:确认|确定|判断|等同)|不等于|不能据此推断", part)
        if (
            not epistemic
            and overlap_ratio(part, closest) >= 0.5
            and bool(_NEGATIVE.search(part)) != bool(_NEGATIVE.search(closest))
        ):
            return "claim reverses negation in the quote"
    return ""


def support_check(
    evidence_id: str,
    claim: str,
    quote: str,
    semantic: CitationCheck | None = None,
    *,
    source_context: str | None = None,
) -> CitationCheck:
    status: Literal["supported", "weak", "unsupported"]
    reason = contradiction(claim, quote, context=source_context)
    if reason:
        status = "unsupported"
    elif direct_support(claim, quote) and (source_context is None or direct_support(claim, source_context)):
        status, reason = "supported", "unchanged extract from the cited text"
    elif semantic is not None and clean_statement(semantic.statement) == clean_statement(claim):
        status, reason = semantic.status, "semantic review: " + semantic.reason
    else:
        status, reason = "weak", "semantic support has not been established; wording overlap is not proof"
    return CitationCheck(evidence_id=evidence_id, statement=claim, status=status, reason=reason)
