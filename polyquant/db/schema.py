"""SQLite schema and initialization."""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.getenv("POLYQUANT_DB_PATH", "polyquant.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS structural_arb_baskets (
    basket_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_type   TEXT NOT NULL,
    poly_market_id  TEXT NOT NULL,
    kalshi_ticker   TEXT NOT NULL,
    poly_side       TEXT NOT NULL,
    kalshi_side     TEXT NOT NULL,
    poly_price      REAL NOT NULL,
    kalshi_price    REAL NOT NULL,
    total_cost      REAL NOT NULL,
    contracts       REAL NOT NULL,
    total_outlay    REAL NOT NULL,
    guaranteed_payout REAL NOT NULL,
    gross_profit    REAL NOT NULL,
    net_profit      REAL NOT NULL,
    worst_case_fee  REAL NOT NULL,
    roi_pct         REAL NOT NULL,
    is_settled      INTEGER DEFAULT 0,
    settlement_profit REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    settled_at      TEXT
);

CREATE TABLE IF NOT EXISTS trading_ledger (
    ledger_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_arb_basket_id INTEGER,
    platform             TEXT NOT NULL,
    market_id            TEXT NOT NULL,
    side                 TEXT NOT NULL,
    price                REAL NOT NULL,
    quantity             REAL NOT NULL,
    stake                REAL NOT NULL,
    status               TEXT DEFAULT 'OPEN',
    net_return           REAL DEFAULT 0.0,
    fill_timestamp       TEXT,
    synced_at            TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_arb_basket_id) REFERENCES structural_arb_baskets(basket_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_basket ON trading_ledger(parent_arb_basket_id);
CREATE INDEX IF NOT EXISTS idx_ledger_platform ON trading_ledger(platform);
CREATE INDEX IF NOT EXISTS idx_baskets_settled ON structural_arb_baskets(is_settled);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA_SQL)
