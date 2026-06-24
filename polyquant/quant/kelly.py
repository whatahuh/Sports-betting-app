"""Liquidity-adjusted fractional Kelly position sizing."""
from __future__ import annotations

LIQUID_VOLUME_THRESHOLD = 50_000.0


def fractional_kelly(
    true_win_prob: float,
    share_price_cents: float,
    volume: float = 0.0,
    *,
    liquid_threshold: float = LIQUID_VOLUME_THRESHOLD,
) -> float:
    """
    Liquidity-adjusted fractional Kelly.
    Half-Kelly for liquid markets, Quarter-Kelly for illiquid/props.
    Returns recommended allocation as % of bankroll.
    """
    p = true_win_prob / 100.0
    c = share_price_cents / 100.0
    if not (0.0 < c < 1.0) or not (0.0 < p <= 1.0):
        return 0.0
    full_kelly = (p - c) / (1.0 - c)
    if full_kelly <= 0:
        return 0.0
    fraction = 0.5 if volume >= liquid_threshold else 0.25
    return max(0.0, min(full_kelly * fraction * 100.0, 25.0))
