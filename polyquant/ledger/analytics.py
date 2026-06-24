"""Ledger analytics -- daily PnL, performance aggregation, and KPIs."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd


def _ledger_daily_pnl(ledger: pd.DataFrame) -> dict[date, float]:
    if ledger.empty:
        return {}
    settled = ledger[ledger["Status"].isin(["WON", "LOST"])].copy()
    if settled.empty:
        return {}
    settled["_day"] = pd.to_datetime(settled["Date"], errors="coerce").dt.date
    grouped = settled.groupby("_day")["Net Return $"].sum()
    return {k: float(v) for k, v in grouped.items() if pd.notna(k)}


def _aggregate_daily_performance(ledger: pd.DataFrame) -> dict[date, float]:
    """Presentation lookup: net win/loss per settlement day."""
    if ledger.empty:
        return {}
    perf = ledger[ledger["Status"].isin(["WON", "LOST"])].copy()
    if perf.empty:
        return {}
    perf["settlement_date"] = pd.to_datetime(perf["Date"], errors="coerce").dt.date
    daily_perf = perf.groupby("settlement_date")["Net Return $"].sum()
    return {k: float(v) for k, v in daily_perf.items() if pd.notna(k)}


def _ledger_daily_bet_counts(ledger: pd.DataFrame) -> dict[date, int]:
    """Presentation lookup: settled bet count per day."""
    if ledger.empty:
        return {}
    settled = ledger[ledger["Status"].isin(["WON", "LOST"])].copy()
    if settled.empty:
        return {}
    settled["settlement_date"] = pd.to_datetime(settled["Date"], errors="coerce").dt.date
    counts = settled.groupby("settlement_date").size()
    return {k: int(v) for k, v in counts.items() if pd.notna(k)}


def _ledger_kpis(ledger: pd.DataFrame) -> tuple[float, str, float]:
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)

    daily_net = 0.0
    if not ledger.empty:
        today_rows = ledger[
            (ledger["Date"] == today.isoformat()) & ledger["Status"].isin(["WON", "LOST"])
        ]
        daily_net = float(today_rows["Net Return $"].sum()) if not today_rows.empty else 0.0

    month_rows = ledger[ledger["Status"].isin(["WON", "LOST"])].copy()
    if not month_rows.empty:
        month_rows["_d"] = pd.to_datetime(month_rows["Date"], errors="coerce").dt.date
        month_rows = month_rows[month_rows["_d"] >= month_start]
        wins = int((month_rows["Net Return $"] > 0).sum())
        losses = int((month_rows["Net Return $"] <= 0).sum())
        wl = f"{wins}W – {losses}L"
    else:
        wl = "0W – 0L"

    open_rows = ledger[ledger["Status"] == "OPEN"]
    capital_at_risk = float(open_rows["Stake $"].sum()) if not open_rows.empty else 0.0
    return daily_net, wl, capital_at_risk
