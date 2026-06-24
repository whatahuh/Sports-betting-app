"""EV calculations, price parsing, and value play enrichment."""
from __future__ import annotations

import json
from typing import Any, Optional

import pandas as pd
import requests

from polyquant.config import (
    PLATFORM_FEE_PCT,
    MIN_VOLUME,
    REQUEST_TIMEOUT,
    USER_AGENT,
    VALUE_PLAYS_EV_EDGE_MIN,
    VALUE_PLAYS_WIN_MIN,
)
from polyquant.quant.shin import model_probability


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def parse_dollar_string(raw: Any) -> Optional[float]:
    """Parse Kalshi V2 fixed-point dollar strings (e.g. ``"0.5250"``)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        cleaned = str(raw).strip()
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def parse_outcome_prices(raw: Any) -> tuple[Optional[float], Optional[float]]:
    """Unwrap Polymarket double-encoded ``outcomePrices`` -> (yes, no)."""
    parsed: Any = raw
    for _ in range(3):
        if isinstance(parsed, str):
            candidate = parsed.strip()
            if not candidate:
                return None, None
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                break
        else:
            break

    if not isinstance(parsed, (list, tuple)) or len(parsed) < 2:
        return None, None

    return _coerce_float(parsed[0]), _coerce_float(parsed[1])


def _api_get(url: str, params: dict[str, Any]) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def calc_ev_dollars(
    true_win_prob: float,
    stake: float,
    share_price_cents: float,
    platform_fee_pct: float = PLATFORM_FEE_PCT,
) -> tuple[float, float]:
    """Fee-adjusted EV: EV = (P_win * (Profit - Fee)) - (P_loss * Stake)."""
    p_win = true_win_prob / 100.0
    p_loss = 1.0 - p_win
    cost = share_price_cents / 100.0
    if cost <= 0:
        return 0.0, 0.0
    shares = stake / cost
    gross_profit = (shares * 1.0) - stake
    fee_on_win = gross_profit * (platform_fee_pct / 100.0)
    net_profit = gross_profit - fee_on_win
    ev_dollars = (p_win * net_profit) - (p_loss * stake)
    ev_yield_pct = (ev_dollars / stake * 100.0) if stake > 0 else 0.0
    return ev_dollars, ev_yield_pct


def net_ev_edge_pct(model_win_pct: float, cost_cents: float, stake: float = 100.0) -> float:
    """Net EV edge % after platform fee."""
    ev_dollars, ev_yield_pct = calc_ev_dollars(model_win_pct, stake, cost_cents)
    return ev_yield_pct


def _signed_money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def enrich_value_plays(df: pd.DataFrame) -> pd.DataFrame:
    """Attach Shin's method win %, gross EV, and fee-adjusted net EV edge to each row."""
    out = df.dropna(subset=["No Price"]).copy()
    if out.empty:
        return out
    # Use Shin's method instead of static 77.5%
    model_probs = out.apply(
        lambda r: model_probability(
            float(r.get("Yes Price", 0) or 0),
            float(r.get("No Price", 0) or 0),
        ),
        axis=1,
    )
    out["Model Win %"] = model_probs.apply(lambda t: t[1] * 100.0)  # NO-side probability
    out["Cost ¢"] = (out["No Price"] * 100.0).round(1)
    ev_cols = out.apply(
        lambda r: calc_ev_dollars(r["Model Win %"], 100.0, r["Cost ¢"]),
        axis=1,
    )
    out["Gross EV $"] = ev_cols.apply(lambda t: t[0])
    out["Gross EV %"] = ev_cols.apply(lambda t: t[1])
    out["Net EV Edge %"] = out["Gross EV %"]  # Already fee-adjusted in calc_ev_dollars
    return out
