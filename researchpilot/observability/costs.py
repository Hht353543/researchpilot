"""Token cost estimation from a configurable price table."""

from __future__ import annotations

from collections.abc import Mapping


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    price_table: Mapping[str, Mapping[str, float]],
) -> float:
    """Return USD cost. Unknown/local/mock models cost 0."""
    prices = price_table.get(model) or price_table.get("default") or {}
    in_price = float(prices.get("input", 0.0))
    out_price = float(prices.get("output", 0.0))
    cost = (prompt_tokens / 1_000_000) * in_price + (completion_tokens / 1_000_000) * out_price
    return round(cost, 8)
