"""CRUD operations for arb baskets and trading ledger."""
from __future__ import annotations

from typing import Any, Optional

from polyquant.db.schema import get_connection


def save_arb_basket(strategy: dict[str, Any], poly_market_id: str, kalshi_ticker: str) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO structural_arb_baskets
               (strategy_type, poly_market_id, kalshi_ticker, poly_side, kalshi_side,
                poly_price, kalshi_price, total_cost, contracts, total_outlay,
                guaranteed_payout, gross_profit, net_profit, worst_case_fee, roi_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy["label"], poly_market_id, kalshi_ticker,
                strategy["poly_side"], strategy["kalshi_side"],
                strategy["poly_price"], strategy["kalshi_price"],
                strategy["total_cost"], strategy["contracts"],
                strategy["total_outlay"], strategy["guaranteed_payout"],
                strategy["gross_profit"], strategy["net_profit"],
                strategy["worst_case_fee"], strategy["roi"],
            ),
        )
        return cursor.lastrowid


def list_arb_baskets(settled: Optional[bool] = None) -> list[dict]:
    with get_connection() as conn:
        if settled is None:
            rows = conn.execute(
                "SELECT * FROM structural_arb_baskets ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM structural_arb_baskets WHERE is_settled = ? ORDER BY created_at DESC",
                (int(settled),),
            ).fetchall()
        return [dict(r) for r in rows]


def settle_basket(basket_id: int, actual_profit: float) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE structural_arb_baskets SET is_settled = 1, settlement_profit = ?, settled_at = datetime('now') WHERE basket_id = ?",
            (actual_profit, basket_id),
        )


def save_ledger_entry(entry: dict[str, Any]) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO trading_ledger
               (parent_arb_basket_id, platform, market_id, side, price, quantity, stake, status, net_return, fill_timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.get("parent_arb_basket_id"),
                entry["platform"], entry["market_id"], entry["side"],
                entry["price"], entry["quantity"], entry["stake"],
                entry.get("status", "OPEN"), entry.get("net_return", 0.0),
                entry.get("fill_timestamp"),
            ),
        )
        return cursor.lastrowid


def get_basket_summary() -> dict:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM structural_arb_baskets").fetchone()[0]
        open_count = conn.execute(
            "SELECT COUNT(*) FROM structural_arb_baskets WHERE is_settled = 0"
        ).fetchone()[0]
        settled_count = conn.execute(
            "SELECT COUNT(*) FROM structural_arb_baskets WHERE is_settled = 1"
        ).fetchone()[0]
        total_pnl_row = conn.execute(
            "SELECT COALESCE(SUM(settlement_profit), 0) FROM structural_arb_baskets WHERE is_settled = 1"
        ).fetchone()
        return {
            "total": total,
            "open": open_count,
            "settled": settled_count,
            "total_pnl": float(total_pnl_row[0]),
        }
