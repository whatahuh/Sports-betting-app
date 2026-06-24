"""Ledger ingestion -- authenticated fills from Kalshi and Polymarket."""
from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st

from polyquant.api.kalshi import _fetch_kalshi_fills_raw
from polyquant.api.polymarket import _fetch_polymarket_trades_raw
from polyquant.config import LEDGER_CACHE_TTL, LEDGER_COLUMNS
from polyquant.quant.ev import _coerce_float


def _ledger_credentials() -> dict[str, bool]:
    return {
        "kalshi": bool(os.getenv("KALSHI_API_KEY_ID") and os.getenv("KALSHI_PRIVATE_KEY")),
        "polymarket": bool(
            os.getenv("POLYMARKET_API_KEY")
            and os.getenv("POLYMARKET_API_SECRET")
            and os.getenv("POLYMARKET_API_PASSPHRASE")
        ),
    }


def _empty_ledger() -> pd.DataFrame:
    return pd.DataFrame(columns=LEDGER_COLUMNS)


def _normalize_kalshi_fill(fill: dict[str, Any]) -> dict[str, Any]:
    ts_raw = fill.get("created_time") or fill.get("ts") or fill.get("created_ts")
    ts = pd.to_datetime(ts_raw, utc=True, errors="coerce")
    price = _coerce_float(fill.get("yes_price_dollars") or fill.get("price")) or 0.0
    if price > 1.0:
        price = price / 100.0
    count = _coerce_float(fill.get("count") or fill.get("quantity") or fill.get("count_fp")) or 0.0
    side = str(fill.get("side") or fill.get("action") or "YES").upper()
    stake = price * count if count else _coerce_float(fill.get("cost_dollars")) or 0.0
    ticker = fill.get("ticker") or fill.get("market_ticker") or "Kalshi Market"
    status = "OPEN"
    result = str(fill.get("result") or "").lower()
    if result in ("yes", "no"):
        won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        status = "WON" if won else "LOST"
    net = _coerce_float(fill.get("pnl_dollars"))
    if net is None and status == "WON":
        net = count - stake
    elif net is None and status == "LOST":
        net = -stake
    elif net is None:
        net = 0.0
    return {
        "Date": ts.strftime("%Y-%m-%d") if pd.notna(ts) else "—",
        "Platform Badge": "K",
        "Event Name": ticker,
        "Position Taken": side,
        "Stake $": round(float(stake), 2),
        "Price Paid ¢": round(float(price) * 100.0, 2),
        "Status": status,
        "Net Return $": round(float(net), 2),
        "_timestamp": ts,
        "_status_raw": status,
    }


def _normalize_polymarket_trade(trade: dict[str, Any]) -> dict[str, Any]:
    ts = pd.to_datetime(trade.get("match_time") or trade.get("timestamp"), utc=True, errors="coerce")
    price = _coerce_float(trade.get("price")) or 0.0
    size = _coerce_float(trade.get("size") or trade.get("amount")) or 0.0
    side = str(trade.get("side") or trade.get("outcome") or "BUY").upper()
    stake = price * size
    market = trade.get("market") or trade.get("asset_id") or trade.get("title") or "Polymarket Trade"
    status = str(trade.get("status") or "OPEN").upper()
    if status not in ("OPEN", "WON", "LOST"):
        status = "OPEN"
    net = _coerce_float(trade.get("realized_pnl") or trade.get("pnl"))
    if net is None:
        net = 0.0
    return {
        "Date": ts.strftime("%Y-%m-%d") if pd.notna(ts) else "—",
        "Platform Badge": "P",
        "Event Name": str(market)[:120],
        "Position Taken": side,
        "Stake $": round(float(stake), 2),
        "Price Paid ¢": round(float(price) * 100.0, 2),
        "Status": status,
        "Net Return $": round(float(net), 2),
        "_timestamp": ts,
        "_status_raw": status,
    }


@st.cache_data(ttl=LEDGER_CACHE_TTL, show_spinner="Syncing filled orders...")
def fetch_unified_ledger() -> pd.DataFrame:
    """Ingest Kalshi + Polymarket fills into one normalized ledger DataFrame."""
    rows: list[dict[str, Any]] = []
    creds = _ledger_credentials()

    if creds["kalshi"]:
        try:
            for fill in _fetch_kalshi_fills_raw():
                if isinstance(fill, dict):
                    rows.append(_normalize_kalshi_fill(fill))
        except Exception:
            pass

    if creds["polymarket"]:
        try:
            for trade in _fetch_polymarket_trades_raw():
                if isinstance(trade, dict):
                    rows.append(_normalize_polymarket_trade(trade))
        except Exception:
            pass

    if not rows:
        return _empty_ledger()

    df = pd.DataFrame(rows)
    df = df.sort_values("_timestamp", ascending=False, na_position="last").reset_index(drop=True)
    return df
