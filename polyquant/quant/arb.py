"""Cross-book arbitrage detection and strategy builder."""
from __future__ import annotations

from typing import Any

from polyquant.config import PLATFORM_FEE_PCT


def arb_opportunity(total_cost: float) -> tuple[float, float]:
    """Return (net_return, roi_pct) for paired position settling at $1.00."""
    net_return = 1.0 - total_cost
    roi_pct = (net_return / total_cost * 100.0) if total_cost > 0 else 0.0
    return net_return, roi_pct


def build_arb_strategy(
    key: str,
    label: str,
    poly_side: str,
    poly_price: float,
    kalshi_side: str,
    kalshi_price: float,
    stake: float,
    poly_fee_pct: float = PLATFORM_FEE_PCT,
) -> dict[str, Any]:
    """Build arb strategy with fee-adjusted profit."""
    total_cost = poly_price + kalshi_price
    contracts = stake
    poly_cash = contracts * poly_price
    kalshi_cash = contracts * kalshi_price
    total_outlay = poly_cash + kalshi_cash
    guaranteed_payout = contracts * 1.0

    poly_profit_if_wins = guaranteed_payout - poly_cash
    kalshi_profit_if_wins = guaranteed_payout - kalshi_cash  # noqa: F841
    poly_fee = max(0, poly_profit_if_wins) * (poly_fee_pct / 100.0)
    worst_case_fee = poly_fee  # Kalshi has no winner fee currently

    gross_profit = guaranteed_payout - total_outlay
    net_profit = gross_profit - worst_case_fee
    is_arb = net_profit > 0
    roi = (net_profit / total_outlay * 100.0) if total_outlay > 0 else 0.0

    return {
        "key": key,
        "label": label,
        "poly_side": poly_side,
        "poly_price": poly_price,
        "kalshi_side": kalshi_side,
        "kalshi_price": kalshi_price,
        "total_cost": total_cost,
        "roi": roi,
        "is_arb": is_arb,
        "contracts": contracts,
        "poly_cash": poly_cash,
        "kalshi_cash": kalshi_cash,
        "total_outlay": total_outlay,
        "guaranteed_payout": guaranteed_payout,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "worst_case_fee": worst_case_fee,
        "break_even_gap": 1.0 - total_cost,
    }


def best_arb_strategy(strategies: list[dict]) -> dict:
    """Pick lowest combined-cost strategy."""
    return min(strategies, key=lambda s: float(s["total_cost"]))
