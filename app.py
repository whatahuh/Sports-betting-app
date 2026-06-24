"""
POLY-QUANT-v1
=============
Executive sports-betting intelligence terminal.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import html
import json
import os
import re
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# =============================================================================
# ** DEVELOPER: Declare ALL API keys, secret passphrases, and private wallet
# keys in a local, git-ignored `.env` file. NEVER commit secrets to git. **
# Required variables:
#   KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY
#   POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
#   POLYMARKET_WALLET_ADDRESS (optional, for trade filtering)
# =============================================================================

# --------------------------------------------------------------------------- #
# Backend — quantitative engine (do not alter logic)
# --------------------------------------------------------------------------- #

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
REQUEST_TIMEOUT = 20
CACHE_TTL = 60
USER_AGENT = "POLY-QUANT-v1/2.0 (+tactical-terminal)"
APP_BUILD = "3.0.1-arb-detail-breakdown"
GIT_SHA = "6f2cc3c"

MIN_VOLUME = 5_000.0
STRIKE_LO = 0.70
STRIKE_HI = 0.85
EV_THRESHOLD = 4.5
WIN_PROB_THRESHOLD = 75.0
DIVERGENCE_TRIGGER = 20.0
DISPLAY_MODEL_WIN_PCT = 77.5

# Top Value Plays — Master SOP gates (presentation / filter layer only)
VALUE_PLAYS_WIN_MIN = 75.0          # strict: model win prob must be > 75%
VALUE_PLAYS_EV_EDGE_MIN = 5.0       # net EV edge >= 5% after platform fees
VALUE_PLAYS_MAX = 5                 # elite tier cap — sharpest edges only
PLATFORM_FEE_PCT = 2.0              # Polymarket winner fee drag on gross profit

COL_QUESTION = 200
COL_PRICE = 72
COL_VOLUME = 88


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
    """Parse Kalshi V2 fixed-point dollar strings (e.g. ``\"0.5250\"``)."""
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
    """Unwrap Polymarket double-encoded ``outcomePrices`` → (yes, no)."""
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


def _calc_ev_dollars(true_win_prob: float, stake: float, share_price_cents: float) -> tuple[float, float]:
    """Net-settlement EV: EV = (P_win * profit) - (P_loss * stake)."""
    p_win = true_win_prob / 100.0
    p_loss = 1.0 - p_win
    cost = share_price_cents / 100.0
    shares = stake / cost if cost > 0 else 0.0
    profit = (shares * 1.0) - stake
    ev_dollars = (p_win * profit) - (p_loss * stake)
    ev_yield_pct = (ev_dollars / stake * 100.0) if stake > 0 else 0.0
    return ev_dollars, ev_yield_pct


def _arb_opportunity(total_cost: float) -> tuple[float, float]:
    """Return (net_return, roi_pct) for a paired position settling at $1.00."""
    net_return = 1.0 - total_cost
    roi_pct = (net_return / total_cost * 100.0) if total_cost > 0 else 0.0
    return net_return, roi_pct


def _select_label(text: str, max_len: int = 72) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= max_len else clean[: max_len - 1] + "…"


def _in_strike_zone(no_price: float) -> bool:
    return STRIKE_LO <= no_price <= STRIKE_HI


def _model_win_pct_for_no_price(no_price: float) -> float:
    """Presentation-layer true probability estimate for a NO position."""
    return DISPLAY_MODEL_WIN_PCT if _in_strike_zone(no_price) else no_price * 100.0


def _net_ev_edge_pct(model_win_pct: float, cost_cents: float, stake: float = 100.0) -> float:
    """
    Net EV edge % after platform fee on winnings.
    Wraps _calc_ev_dollars — does not alter the core settlement formula.
    """
    gross_ev, _ = _calc_ev_dollars(model_win_pct, stake, cost_cents)
    p_win = model_win_pct / 100.0
    cost = cost_cents / 100.0
    shares = stake / cost if cost > 0 else 0.0
    profit = (shares * 1.0) - stake
    fee_drag = p_win * profit * (PLATFORM_FEE_PCT / 100.0)
    net_ev = gross_ev - fee_drag
    return (net_ev / stake) * 100.0 if stake > 0 else 0.0


def _enrich_value_plays(df: pd.DataFrame) -> pd.DataFrame:
    """Attach model win %, gross EV, and fee-adjusted net EV edge to each row."""
    out = df.dropna(subset=["No Price"]).copy()
    if out.empty:
        return out

    out["Model Win %"] = out["No Price"].apply(_model_win_pct_for_no_price)
    out["Cost ¢"] = (out["No Price"] * 100.0).round(1)
    ev_cols = out.apply(
        lambda r: _calc_ev_dollars(r["Model Win %"], 100.0, r["Cost ¢"]), axis=1
    )
    out["Gross EV $"] = ev_cols.apply(lambda t: t[0])
    out["Gross EV %"] = ev_cols.apply(lambda t: t[1])
    out["Net EV Edge %"] = out.apply(
        lambda r: _net_ev_edge_pct(r["Model Win %"], r["Cost ¢"]), axis=1
    )
    return out


def _parse_kalshi_market_row(market: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Kalshi V2 market record (unchanged pricing logic)."""
    yes_ask = parse_dollar_string(market.get("yes_ask_dollars"))
    yes_bid = parse_dollar_string(market.get("yes_bid_dollars"))
    kalshi_yes_cost = yes_ask
    kalshi_no_cost = (1.0 - yes_bid) if yes_bid is not None else None
    title = market.get("title") or market.get("ticker") or "—"
    ticker = market.get("ticker") or title
    return {
        "ticker": ticker,
        "Title": title,
        "Yes Ask": kalshi_yes_cost,
        "Yes Bid": yes_bid,
        "Kalshi YES Cost": kalshi_yes_cost,
        "Kalshi NO Cost": kalshi_no_cost,
        "event_ticker": market.get("event_ticker") or "",
        "series_ticker": market.get("series_ticker") or ticker.split("-")[0],
    }


KALSHI_PROP_SERIES = (
    "KXMLBHIT",
    "KXNBAH2H3PT",
    "KXNFLANYTD",
    "KXNFLRSHYDS",
    "KXNBAPTS",
    "KXNBAREB",
    "KXNBAAST",
)


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading live markets...")
def fetch_polymarket_markets() -> pd.DataFrame:
    payload = _api_get(
        GAMMA_MARKETS_URL,
        {"active": "true", "closed": "false", "limit": 100},
    )

    if isinstance(payload, dict):
        markets = payload.get("data") or payload.get("markets") or []
    else:
        markets = payload

    rows: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue

        yes_price, no_price = parse_outcome_prices(market.get("outcomePrices"))
        volume = _coerce_float(
            market.get("volume") or market.get("volumeNum") or market.get("volumeClob")
        )
        liquidity = _coerce_float(market.get("liquidity") or market.get("liquidityNum"))
        question = (
            market.get("question")
            or market.get("title")
            or market.get("slug")
            or "—"
        )
        events = market.get("events") or []
        event_title = ""
        if events and isinstance(events[0], dict):
            event_title = events[0].get("title") or ""
        slug = market.get("slug") or ""
        group_item = market.get("groupItemTitle") or ""

        rows.append(
            {
                "id": str(market.get("id") or market.get("conditionId") or question),
                "Question": question,
                "Yes Price": yes_price,
                "No Price": no_price,
                "Volume": volume if volume is not None else 0.0,
                "Liquidity": liquidity if liquidity is not None else 0.0,
                "Event Title": event_title,
                "Slug": slug,
                "Group Item": group_item,
            }
        )

    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading exchange prices...")
def fetch_kalshi_markets() -> pd.DataFrame:
    payload = _api_get(KALSHI_MARKETS_URL, {"status": "open", "limit": 100})

    if isinstance(payload, dict):
        markets = payload.get("markets") or []
    else:
        markets = payload

    rows: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        rows.append(_parse_kalshi_market_row(market))

    return pd.DataFrame(rows)


@st.cache_data(ttl=CACHE_TTL, show_spinner="Loading player props...")
def fetch_kalshi_player_props() -> pd.DataFrame:
    """Supplementary Kalshi series fetch for player-prop style markets."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for series in KALSHI_PROP_SERIES:
        try:
            payload = _api_get(
                KALSHI_MARKETS_URL,
                {"status": "open", "series_ticker": series, "limit": 100},
            )
            for market in payload.get("markets", []):
                if not isinstance(market, dict):
                    continue
                ticker = str(market.get("ticker") or "")
                if not ticker or ticker in seen:
                    continue
                seen.add(ticker)
                rows.append(_parse_kalshi_market_row(market))
        except requests.exceptions.RequestException:
            continue
    return pd.DataFrame(rows)


def _filter_value_plays(raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Master SOP filter: win prob > 75%, net EV edge >= 5%, sort DESC by edge, cap at 5.
    """
    out = _enrich_value_plays(raw_df)
    if out.empty:
        return out

    out = out[out["Volume"] >= MIN_VOLUME]
    out = out[out["Model Win %"] > VALUE_PLAYS_WIN_MIN]
    out = out[out["Net EV Edge %"] >= VALUE_PLAYS_EV_EDGE_MIN]
    return (
        out.sort_values("Net EV Edge %", ascending=False)
        .reset_index(drop=True)
        .head(VALUE_PLAYS_MAX)
    )


# --------------------------------------------------------------------------- #
# Ledger ingestion — authenticated fills (Phase 3, additive only)
# --------------------------------------------------------------------------- #

KALSHI_API_BASE = os.getenv("KALSHI_API_BASE", "https://api.elections.kalshi.com")
POLYMARKET_CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
LEDGER_CACHE_TTL = 120
DEFAULT_ARB_STAKE = 100.0

LEDGER_COLUMNS = [
    "Date",
    "Platform Badge",
    "Event Name",
    "Position Taken",
    "Stake $",
    "Price Paid ¢",
    "Status",
    "Net Return $",
    "_timestamp",
    "_status_raw",
]

# Official API key pages — surfaced in The Ledger setup UI
KALSHI_KEYS_PAGE = "https://kalshi.com/account/profile"
KALSHI_KEYS_DOCS = "https://docs.kalshi.com/getting_started/api_keys"
POLYMARKET_AUTH_DOCS = "https://docs.polymarket.com/api-reference/authentication"
POLYMARKET_TRADING_DOCS = "https://docs.polymarket.com/trading/overview"
STREAMLIT_SECRETS_DOCS = "https://docs.streamlit.io/develop/concepts/connections/secrets-management"


def _ledger_credentials() -> dict[str, bool]:
    return {
        "kalshi": bool(os.getenv("KALSHI_API_KEY_ID") and os.getenv("KALSHI_PRIVATE_KEY")),
        "polymarket": bool(
            os.getenv("POLYMARKET_API_KEY")
            and os.getenv("POLYMARKET_API_SECRET")
            and os.getenv("POLYMARKET_API_PASSPHRASE")
        ),
    }


def _kalshi_auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    pem = os.getenv("KALSHI_PRIVATE_KEY", "")
    if not key_id or not pem or not _CRYPTO_AVAILABLE:
        return {}
    pem = pem.replace("\\n", "\n")
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sign_path = path.split("?")[0]
    message = f"{timestamp}{method.upper()}{sign_path}".encode("utf-8")
    try:
        private_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    except Exception:
        return {}
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _poly_clob_auth_headers(method: str, request_path: str, body: str = "") -> dict[str, str]:
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    if not all([api_key, api_secret, api_passphrase]):
        return {}
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    message = timestamp + method.upper() + request_path + body
    try:
        hmac_key = base64.b64decode(api_secret)
        signature = hmac.new(hmac_key, message.encode("utf-8"), hashlib.sha256).digest()
    except Exception:
        return {}
    headers = {
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": api_passphrase,
        "POLY_TIMESTAMP": timestamp,
        "POLY_SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Accept": "application/json",
    }
    wallet = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
    if wallet:
        headers["POLY_ADDRESS"] = wallet
    return headers


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


def _fetch_kalshi_fills_raw() -> list[dict[str, Any]]:
    path = "/trade-api/v2/portfolio/fills"
    headers = _kalshi_auth_headers("GET", path)
    if not headers:
        return []
    url = f"{KALSHI_API_BASE.rstrip('/')}{path}"
    response = requests.get(url, headers=headers, params={"limit": 200}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload.get("fills") or payload.get("data") or []


def _fetch_polymarket_trades_raw() -> list[dict[str, Any]]:
    path = "/data/trades"
    headers = _poly_clob_auth_headers("GET", path)
    if not headers:
        return []
    url = f"{POLYMARKET_CLOB_URL.rstrip('/')}{path}"
    response = requests.get(url, headers=headers, params={"limit": 200}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return payload
    return payload.get("trades") or payload.get("data") or []


@st.cache_data(ttl=LEDGER_CACHE_TTL, show_spinner="Syncing filled orders…")
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



# --------------------------------------------------------------------------- #
# Presentation layer — plain English, visual hierarchy only
# --------------------------------------------------------------------------- #

ODDS_FORMATS = ("Cents", "Percentage", "American")
PICKER_PAGE_SIZE = 6
EXPLORE_PAGE_SIZE = 8

EXPLORE_CATEGORIES = (
    "All",
    "Sports",
    "Player Props",
    "Politics",
    "Crypto",
    "Pop Culture",
)
EXPLORE_SPORTS_TYPES = ("All", "Matchups", "Game Lines", "Futures", "Parlays")
EXPLORE_SOURCES = ("Both", "Polymarket", "Kalshi")


def _classify_market(
    title: str,
    *,
    source: str = "",
    series_ticker: str = "",
    event_title: str = "",
) -> tuple[str, str]:
    """Presentation-only taxonomy for Pikkit-style browse navigation."""
    blob = f"{title} {event_title} {series_ticker} {source}".lower()
    series_u = series_ticker.upper()

    if series_u in KALSHI_PROP_SERIES or any(
        k in blob for k in (": 1+", ": 2+", ": 3+", " hits?", " strikeouts", " touchdowns", " points?")
    ):
        return "Player Props", "Props"

    if "kxmv" in series_u.lower() or title.count(",") >= 3:
        return "Sports", "Parlays"

    sports_kw = (
        "mlb", "nfl", "nba", "nhl", "fifa", "world cup", "super bowl", "mvp",
        "championship", "playoff", " vs ", "beat", "win the", "match", "game",
        "serie a", "premier league", "march madness", "pga", "ufc", "boxing",
    )
    futures_kw = ("before 20", "by 20", "in 202", "203", "win the 202", "nomination")

    if any(k in blob for k in sports_kw):
        if any(k in blob for k in futures_kw):
            return "Sports", "Futures"
        if any(k in blob for k in ("o/u", "over", "under", "spread", "total", "moneyline")):
            return "Sports", "Game Lines"
        return "Sports", "Matchups"

    politics_kw = (
        "president", "election", "congress", "senate", "trump", "biden",
        "democrat", "republican", "nomination", "governor", "parliament",
    )
    crypto_kw = ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "token")
    pop_kw = ("album", "gta", "oscar", "grammy", "movie", "taylor swift", "kardashian", "rihanna")

    if any(k in blob for k in politics_kw):
        return "Politics", "General"
    if any(k in blob for k in crypto_kw):
        return "Crypto", "General"
    if any(k in blob for k in pop_kw):
        return "Pop Culture", "General"
    return "Other", "General"


@st.cache_data(ttl=CACHE_TTL, show_spinner="Building market catalog...")
def build_explore_catalog() -> pd.DataFrame:
    """Unified Polymarket + Kalshi browse index for search and category navigation."""
    frames: list[pd.DataFrame] = []

    try:
        poly = fetch_polymarket_markets()
        if not poly.empty:
            p = poly.dropna(subset=["Yes Price", "No Price"]).copy()
            cat_sub = p.apply(
                lambda r: _classify_market(
                    str(r["Question"]),
                    source="polymarket",
                    event_title=str(r.get("Event Title", "")),
                ),
                axis=1,
                result_type="expand",
            )
            p["Category"] = cat_sub[0]
            p["Subcategory"] = cat_sub[1]
            p["Source"] = "Polymarket"
            p["Catalog ID"] = "poly:" + p["id"].astype(str)
            p["Title"] = p["Question"]
            frames.append(
                p[
                    [
                        "Catalog ID", "Source", "id", "Title", "Yes Price", "No Price",
                        "Volume", "Category", "Subcategory", "Event Title",
                    ]
                ]
            )
    except Exception:
        pass

    kalshi_frames: list[pd.DataFrame] = []
    try:
        kalshi_frames.append(fetch_kalshi_markets())
    except Exception:
        pass
    try:
        props = fetch_kalshi_player_props()
        if not props.empty:
            kalshi_frames.append(props)
    except Exception:
        pass

    if kalshi_frames:
        k = pd.concat(kalshi_frames, ignore_index=True).drop_duplicates(subset=["ticker"])
        k = k.dropna(subset=["Kalshi YES Cost", "Kalshi NO Cost"]).copy()
        k["Yes Price"] = k["Kalshi YES Cost"]
        k["No Price"] = k["Kalshi NO Cost"]
        k["Volume"] = 0.0
        cat_sub = k.apply(
            lambda r: _classify_market(
                str(r["Title"]),
                source="kalshi",
                series_ticker=str(r.get("series_ticker", "")),
            ),
            axis=1,
            result_type="expand",
        )
        k["Category"] = cat_sub[0]
        k["Subcategory"] = cat_sub[1]
        k["Source"] = "Kalshi"
        k["Catalog ID"] = "kalshi:" + k["ticker"].astype(str)
        k["id"] = k["ticker"]
        k["Event Title"] = ""
        frames.append(
            k[
                [
                    "Catalog ID", "Source", "id", "Title", "Yes Price", "No Price",
                    "Volume", "Category", "Subcategory", "Event Title",
                ]
            ]
        )

    if not frames:
        return pd.DataFrame()

    catalog = pd.concat(frames, ignore_index=True)
    catalog["Search Blob"] = (
        catalog["Title"].astype(str)
        + " "
        + catalog["Event Title"].astype(str)
        + " "
        + catalog["Category"].astype(str)
        + " "
        + catalog["Subcategory"].astype(str)
    ).str.lower()
    return catalog.sort_values(["Category", "Title"]).reset_index(drop=True)


def _init_session() -> None:
    if "odds_format" not in st.session_state:
        st.session_state.odds_format = "American"
    if "poly_selected" not in st.session_state:
        st.session_state.poly_selected = None
    if "kalshi_selected" not in st.session_state:
        st.session_state.kalshi_selected = None
    if "global_search_query" not in st.session_state:
        st.session_state.global_search_query = ""
    if "explore_category" not in st.session_state:
        st.session_state.explore_category = "All"
    if "explore_sports_type" not in st.session_state:
        st.session_state.explore_sports_type = "All"
    if "explore_source" not in st.session_state:
        st.session_state.explore_source = "Both"
    if "explore_page" not in st.session_state:
        st.session_state.explore_page = 0
    if "arb_poly_anchor" not in st.session_state:
        st.session_state.arb_poly_anchor = None


def get_odds_format() -> str:
    return str(st.session_state.get("odds_format", "American")).lower()


def _price_to_american(prob: float) -> str:
    if prob <= 0 or prob >= 1:
        return "—"
    if prob >= 0.5:
        return f"{-100 * prob / (1 - prob):.0f}"
    return f"+{100 * (1 - prob) / prob:.0f}"


def format_odds_display(price: Optional[float], fmt: Optional[str] = None) -> str:
    """Render a contract price in the user's chosen odds format."""
    if price is None or pd.isna(price) or price <= 0 or price >= 1:
        return "—"
    mode = (fmt or get_odds_format()).lower()
    if mode == "cents":
        return f"{price * 100:.1f}¢"
    if mode == "percentage":
        return f"{price * 100:.1f}%"
    return _price_to_american(price)


def parse_offered_odds(raw: Any, input_fmt: str) -> Optional[float]:
    """Convert user-entered odds → share price in cents (for EV engine)."""
    mode = input_fmt.lower()
    if mode == "american":
        text = str(raw).strip().replace(" ", "")
        if not text:
            return None
        if text.startswith("+"):
            text = text[1:]
        try:
            american = float(text)
        except ValueError:
            return None
        if american == 0:
            return None
        prob = (100.0 / (american + 100.0)) if american > 0 else (
            abs(american) / (abs(american) + 100.0)
        )
        return prob * 100.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if mode == "percentage":
        return val
    return val  # cents


def _filter_kalshi_tradeable(df: pd.DataFrame) -> pd.DataFrame:
    """Presentation filter: drop zero-priced combo legs; prefer readable singles."""
    priced = df.dropna(subset=["Kalshi YES Cost", "Kalshi NO Cost"]).copy()
    tradeable = priced[
        (priced["Kalshi YES Cost"] > 0.01)
        & (priced["Kalshi YES Cost"] < 0.99)
        & (priced["Kalshi NO Cost"] > 0.01)
        & (priced["Kalshi NO Cost"] < 0.99)
    ].copy()
    pool = tradeable if not tradeable.empty else priced
    pool = pool.copy()
    pool["_title_len"] = pool["Title"].astype(str).str.len()
    return pool.sort_values("_title_len").drop(columns=["_title_len"]).reset_index(drop=True)


def _short_title(text: str, limit: int = 52) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rsplit(" ", 1)[0] + "…"


_MATCH_STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "of", "in", "on", "by", "before", "after",
    "be", "is", "at", "or", "and", "for", "than", "that", "this", "with",
})


def _normalize_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", str(text).lower())


def _tokenize_for_match(text: str) -> set[str]:
    return {
        t for t in _normalize_match_text(text).split()
        if len(t) > 1 and t not in _MATCH_STOPWORDS
    }


def _title_match_score(poly_title: str, kalshi_title: str) -> float:
    """Fuzzy match score 0–1 between Polymarket and Kalshi market titles."""
    poly_tokens = _tokenize_for_match(poly_title)
    kalshi_tokens = _tokenize_for_match(kalshi_title)
    if not poly_tokens or not kalshi_tokens:
        return 0.0

    overlap = len(poly_tokens & kalshi_tokens) / max(len(poly_tokens), 1)
    seq = SequenceMatcher(
        None,
        _normalize_match_text(poly_title),
        _normalize_match_text(kalshi_title),
    ).ratio()

    nums_poly = set(re.findall(r"\d{4}|\d+", poly_title))
    nums_kalshi = set(re.findall(r"\d{4}|\d+", kalshi_title))
    num_bonus = 0.18 if nums_poly & nums_kalshi else 0.0

    substring_bonus = 0.12 if _normalize_match_text(poly_title)[:24] in _normalize_match_text(kalshi_title) else 0.0

    return min(1.0, 0.50 * overlap + 0.30 * seq + num_bonus + substring_bonus)


def _rank_kalshi_for_poly(
    poly_title: str,
    kalshi_df: pd.DataFrame,
    top_n: int = 5,
) -> list[tuple[float, str, str]]:
    ranked: list[tuple[float, str, str]] = []
    for _, row in kalshi_df.iterrows():
        ticker = str(row["ticker"])
        title = str(row["Title"])
        score = _title_match_score(poly_title, title)
        if score >= 0.12:
            ranked.append((score, ticker, title))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[:top_n]


def _sync_kalshi_auto_suggest(
    poly_id: str,
    poly_title: str,
    kalshi_priced: pd.DataFrame,
) -> list[tuple[float, str, str]]:
    """When Polymarket pick changes, rank Kalshi matches and auto-select best."""
    suggestions = _rank_kalshi_for_poly(poly_title, kalshi_priced)
    st.session_state.arb_kalshi_suggestions = suggestions

    if poly_id != st.session_state.get("arb_poly_anchor"):
        st.session_state.arb_poly_anchor = poly_id
        if suggestions:
            best_score, best_ticker, _ = suggestions[0]
            if best_score >= 0.20:
                st.session_state.kalshi_selected = best_ticker
            st.session_state.kalshi_selected_page = 0
            seed_tokens = list(_tokenize_for_match(poly_title))[:6]
            if seed_tokens:
                st.session_state.kalshi_selected_search = " ".join(seed_tokens)

    return suggestions


def _render_kalshi_suggestions(
    suggestions: list[tuple[float, str, str]],
    kalshi_prices: dict[str, str],
) -> None:
    if not suggestions:
        st.caption("No close Kalshi title matches — search manually below.")
        return

    st.markdown(
        '<p class="pq-section-label">Suggested Kalshi matches for your Polymarket pick</p>',
        unsafe_allow_html=True,
    )
    for score, ticker, title in suggestions[:5]:
        prices = kalshi_prices.get(ticker, "")
        pct = f"{score * 100:.0f}% match"
        st.markdown(
            f"""
            <div class="pq-suggest-card">
                <span class="pq-suggest-score">{html.escape(pct)}</span>
                <span class="pq-suggest-title">{html.escape(_short_title(title, 64))}</span>
                <span class="pq-suggest-meta">{html.escape(prices)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "Use this Kalshi market" if st.session_state.get("kalshi_selected") != ticker else "✓ Selected",
            key=f"kalshi_suggest_{ticker}",
            use_container_width=True,
            type="primary" if st.session_state.get("kalshi_selected") == ticker else "secondary",
        ):
            st.session_state.kalshi_selected = ticker
            st.rerun()


def render_odds_format_toggle() -> None:
    st.segmented_control(
        "Odds Display",
        options=list(ODDS_FORMATS),
        key="odds_format",
        label_visibility="collapsed",
    )


def render_searchable_picker(
    label: str,
    options: dict[str, str],
    session_key: str,
    *,
    show_prices: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """
  Mobile-friendly market picker: search → paginated tap-to-select cards.
  Replaces native selectbox long-list UX.
    """
    if not options:
        st.warning(f"No {label} markets available.")
        return None

    ids = list(options.keys())
    if st.session_state.get(session_key) not in options:
        st.session_state[session_key] = ids[0]

    page_key = f"{session_key}_page"
    if page_key not in st.session_state:
        st.session_state[page_key] = 0

    st.markdown(f'<p class="pq-section-label">{label}</p>', unsafe_allow_html=True)
    search_col, _ = st.columns([3, 1])
    with search_col:
        query = st.text_input(
            f"Search {label}",
            key=f"{session_key}_search",
            placeholder="Search by keyword…",
            label_visibility="collapsed",
        ).lower().strip()
    if not query:
        query = st.session_state.get("global_search_query", "").strip().lower()

    filtered = [(k, v) for k, v in options.items() if not query or query in v.lower()]
    filtered.sort(key=lambda item: len(item[1]))

    if not filtered:
        st.caption("No matches — try a different search.")
        return st.session_state.get(session_key)

    total_pages = max(1, (len(filtered) + PICKER_PAGE_SIZE - 1) // PICKER_PAGE_SIZE)
    page = min(st.session_state[page_key], total_pages - 1)
    st.session_state[page_key] = page
    start = page * PICKER_PAGE_SIZE
    page_items = filtered[start : start + PICKER_PAGE_SIZE]

    st.caption(f"{len(filtered)} results · tap to select")

    for mid, title in page_items:
        selected = st.session_state[session_key] == mid
        price_hint = ""
        if show_prices and mid in show_prices:
            price_hint = f" · {show_prices[mid]}"
        card_cls = "pq-pick-card pq-pick-selected" if selected else "pq-pick-card"
        st.markdown(
            f'<div class="{card_cls}"><span class="pq-pick-title">'
            f'{html.escape(_short_title(title))}</span>'
            f'<span class="pq-pick-meta">{html.escape(price_hint.lstrip(" · "))}</span></div>',
            unsafe_allow_html=True,
        )
        if st.button(
            "✓ Selected" if selected else "Select",
            key=f"pick_{session_key}_{mid}_{page}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            st.session_state[session_key] = mid
            st.rerun()

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        if st.button("←", key=f"{session_key}_prev", disabled=page == 0):
            st.session_state[page_key] = page - 1
            st.rerun()
    with nav2:
        st.markdown(
            f'<p class="pq-page-indicator">Page {page + 1} of {total_pages}</p>',
            unsafe_allow_html=True,
        )
    with nav3:
        if st.button("→", key=f"{session_key}_next", disabled=page >= total_pages - 1):
            st.session_state[page_key] = page + 1
            st.rerun()

    sel_id = st.session_state[session_key]
    st.markdown(
        f'<div class="pq-selected-banner"><strong>Selected:</strong> '
        f'{html.escape(options.get(sel_id, ""))}</div>',
        unsafe_allow_html=True,
    )
    return sel_id


def _inject_global_css() -> None:
    st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            .stApp {
                background: #000000;
                color: #f0f2f5;
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            }

            .block-container {
                padding: 0.5rem 0.85rem 1.5rem;
                max-width: 100%;
            }

            /* Compact top bar (replaces bulky hero) */
            .pq-topbar {
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 0.35rem 0.75rem;
                padding: 0.15rem 0 0.5rem;
                margin-bottom: 0.35rem;
                border-bottom: 1px solid #21262d;
            }
            .pq-topbar-brand {
                font-size: 1rem;
                font-weight: 800;
                letter-spacing: -0.02em;
                color: #ffffff;
            }
            .pq-topbar-meta {
                font-size: 0.72rem;
                font-weight: 500;
                color: #8b949e;
            }

            /* Header (legacy hero — unused) */
            .pq-hero {
                background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 0.85rem 1.1rem;
                margin-bottom: 0.65rem;
            }
            .pq-hero h1 {
                margin: 0;
                font-size: 1.15rem;
                font-weight: 800;
                letter-spacing: -0.02em;
                color: #ffffff;
            }
            .pq-hero p {
                margin: 0.2rem 0 0;
                font-size: 0.78rem;
                color: #8b949e;
                font-weight: 500;
            }

            /* Tabs */
            .stTabs [data-baseweb="tab-list"] {
                gap: 6px;
                background: transparent;
                border-bottom: 1px solid #21262d;
                padding-bottom: 0;
            }
            .stTabs [data-baseweb="tab"] {
                background: transparent;
                color: #8b949e;
                font-weight: 600;
                font-size: 0.82rem;
                padding: 8px 14px;
                border-radius: 8px 8px 0 0;
                border: none;
            }
            .stTabs [aria-selected="true"] {
                color: #58a6ff !important;
                background: #161b22 !important;
                border-bottom: 2px solid #58a6ff !important;
            }

            /* Cards */
            .pq-card {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 0.9rem 1rem;
                margin-bottom: 0.55rem;
            }
            .pq-card-compound {
                border-color: #238636;
                background: linear-gradient(135deg, rgba(35,134,54,0.15) 0%, #161b22 60%);
                box-shadow: 0 0 20px rgba(63,185,80,0.12);
            }
            .pq-card-title {
                font-size: 0.92rem;
                font-weight: 700;
                color: #f0f2f5;
                line-height: 1.35;
                margin: 0 0 0.55rem;
            }
            .pq-card-row {
                display: flex;
                flex-wrap: wrap;
                gap: 0.45rem;
                align-items: center;
            }

            /* Badges */
            .pq-badge {
                display: inline-block;
                padding: 0.22rem 0.55rem;
                border-radius: 20px;
                font-size: 0.7rem;
                font-weight: 700;
                letter-spacing: 0.02em;
                white-space: nowrap;
            }
            .pq-badge-green {
                background: rgba(63,185,80,0.22);
                color: #3fb950;
                border: 1px solid rgba(63,185,80,0.45);
            }
            .pq-badge-blue {
                background: rgba(88,166,255,0.15);
                color: #58a6ff;
                border: 1px solid rgba(88,166,255,0.35);
            }
            .pq-badge-grey {
                background: #21262d;
                color: #8b949e;
                border: 1px solid #30363d;
            }
            .pq-badge-red {
                background: rgba(248,81,73,0.15);
                color: #f85149;
                border: 1px solid rgba(248,81,73,0.35);
            }
            .pq-stat {
                font-size: 0.78rem;
                color: #8b949e;
            }
            .pq-stat strong {
                color: #f0f2f5;
                font-weight: 700;
            }

            /* Verdict containers */
            .pq-verdict-play {
                background: linear-gradient(135deg, rgba(63,185,80,0.25) 0%, rgba(35,134,54,0.12) 100%);
                border: 2px solid #3fb950;
                border-radius: 14px;
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
                box-shadow: 0 0 28px rgba(63,185,80,0.2);
            }
            .pq-verdict-play h2 {
                margin: 0 0 0.35rem;
                font-size: 1.35rem;
                font-weight: 800;
                color: #3fb950;
            }
            .pq-verdict-play p {
                margin: 0;
                font-size: 0.95rem;
                color: #c9d1d9;
                line-height: 1.5;
            }
            .pq-verdict-pass {
                background: #161b22;
                border: 1px solid #30363d;
                border-radius: 14px;
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
            }
            .pq-verdict-pass h2 {
                margin: 0 0 0.35rem;
                font-size: 1.2rem;
                font-weight: 800;
                color: #8b949e;
            }
            .pq-verdict-pass p {
                margin: 0;
                font-size: 0.88rem;
                color: #6e7681;
            }

            /* Arb split */
            .pq-split {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 0.65rem;
                margin: 0.65rem 0;
            }
            @media (max-width: 640px) {
                .pq-split { grid-template-columns: 1fr; }
            }
            .pq-split-side {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 0.85rem;
                text-align: center;
            }
            .pq-split-side .venue {
                font-size: 0.68rem;
                font-weight: 700;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.35rem;
            }
            .pq-split-side .leg {
                font-size: 1rem;
                font-weight: 800;
                color: #58a6ff;
            }
            .pq-arb-banner {
                background: linear-gradient(90deg, rgba(63,185,80,0.3), rgba(35,134,54,0.15));
                border: 2px solid #3fb950;
                border-radius: 12px;
                padding: 1rem 1.1rem;
                text-align: center;
                margin-top: 0.65rem;
            }
            .pq-arb-banner h3 {
                margin: 0 0 0.25rem;
                color: #3fb950;
                font-size: 1.05rem;
                font-weight: 800;
            }
            .pq-arb-banner p {
                margin: 0;
                color: #c9d1d9;
                font-size: 0.9rem;
            }

            /* Warning banner */
            .pq-trap-banner {
                background: linear-gradient(135deg, rgba(248,81,73,0.2), rgba(139,69,19,0.1));
                border: 2px solid #f85149;
                border-radius: 12px;
                padding: 1.1rem 1.2rem;
                margin-top: 0.75rem;
            }
            .pq-trap-banner h3 {
                margin: 0 0 0.4rem;
                color: #f85149;
                font-size: 1rem;
                font-weight: 800;
            }
            .pq-trap-banner p {
                margin: 0;
                color: #c9d1d9;
                font-size: 0.88rem;
                line-height: 1.45;
            }

            /* Input card */
            .pq-input-card {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 0.85rem 1rem 0.25rem;
                margin-bottom: 0.75rem;
            }

            /* Streamlit widgets */
            [data-testid="stMetric"] {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 10px;
                padding: 0.6rem 0.75rem;
            }
            [data-testid="stDataFrame"] {
                border: 1px solid #21262d;
                border-radius: 12px;
            }
            .stSlider label, .stNumberInput label, .stSelectbox label {
                font-weight: 600 !important;
                font-size: 0.82rem !important;
            }
            hr {
                border-color: #21262d;
                margin: 0.75rem 0;
            }

            /* Section labels & picker */
            .pq-section-label {
                font-size: 0.72rem;
                font-weight: 700;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin: 0.65rem 0 0.35rem;
            }
            .pq-pick-card {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 10px;
                padding: 0.55rem 0.75rem;
                margin-bottom: 0.25rem;
            }
            .pq-pick-selected {
                border-color: #58a6ff;
                background: rgba(88,166,255,0.08);
            }
            .pq-pick-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 600;
                color: #f0f2f5;
                line-height: 1.35;
            }
            .pq-pick-meta {
                display: block;
                font-size: 0.72rem;
                color: #58a6ff;
                font-weight: 700;
                margin-top: 0.15rem;
            }
            .pq-page-indicator {
                text-align: center;
                font-size: 0.75rem;
                color: #8b949e;
                margin: 0.35rem 0 0;
            }
            .pq-selected-banner {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 10px;
                padding: 0.65rem 0.8rem;
                font-size: 0.78rem;
                color: #c9d1d9;
                line-height: 1.4;
                margin: 0.5rem 0 0.75rem;
            }
            .pq-odds-bar {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 0.55rem 0.75rem 0.35rem;
                margin-bottom: 0.65rem;
            }

            /* Tactile buttons */
            .stButton > button {
                border-radius: 10px !important;
                font-weight: 600 !important;
                min-height: 2.35rem;
            }
            .stButton > button[kind="secondary"] {
                background: #21262d !important;
                border: 1px solid #30363d !important;
                color: #c9d1d9 !important;
            }
            .stButton > button[kind="primary"] {
                background: #1f6feb !important;
                border: 1px solid #388bfd !important;
            }

            /* Segmented control polish */
            [data-testid="stSegmentedControl"] {
                background: #0d1117;
                border-radius: 10px;
                padding: 3px;
            }

            /* Pikkit-style explore feed */
            .pq-search-hero {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 0.65rem 0.85rem;
                margin-bottom: 0.55rem;
            }
            .pq-feed-row {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 0.75rem 0.85rem;
                margin-bottom: 0.45rem;
            }
            .pq-feed-meta {
                display: block;
                font-size: 0.65rem;
                font-weight: 700;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.25rem;
            }
            .pq-feed-title {
                display: block;
                font-size: 0.88rem;
                font-weight: 700;
                color: #f0f2f5;
                line-height: 1.35;
            }
            .pq-feed-event {
                display: block;
                font-size: 0.72rem;
                color: #6e7681;
                margin-top: 0.2rem;
            }
            .pq-odd-pill {
                display: block;
                text-align: center;
                padding: 0.55rem 0.35rem;
                border-radius: 10px;
                font-weight: 800;
                font-size: 0.95rem;
            }
            .pq-odd-yes {
                background: #1c2d41;
                color: #58a6ff;
                border: 1px solid #30363d;
            }
            .pq-odd-no {
                background: #1a2332;
                color: #c9d1d9;
                border: 1px solid #30363d;
            }
            .pq-nav-scroll .stPills {
                overflow-x: auto;
            }

            /* Phase 1 — tactile value cards */
            .pq-value-card {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 1rem 1.1rem;
                margin-bottom: 0.65rem;
            }
            .pq-value-card-hot {
                border-color: #238636;
                box-shadow: 0 0 18px rgba(63,185,80,0.15);
            }
            .pq-event-name {
                font-size: 0.95rem;
                font-weight: 800;
                color: #f0f2f5;
                margin: 0 0 0.65rem;
                line-height: 1.35;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(90deg, #1f6feb, #388bfd);
                color: #fff;
                font-weight: 800;
                font-size: 0.82rem;
                padding: 0.45rem 0.85rem;
                border-radius: 999px;
                margin-bottom: 0.55rem;
                letter-spacing: 0.02em;
            }
            .pq-ev-badge {
                display: inline-block;
                background: rgba(63,185,80,0.25);
                color: #3fb950;
                border: 1px solid #3fb950;
                font-weight: 800;
                font-size: 0.8rem;
                padding: 0.3rem 0.65rem;
                border-radius: 8px;
            }
            .pq-metric-row {
                display: flex;
                gap: 1.25rem;
                flex-wrap: wrap;
                font-size: 0.78rem;
                color: #8b949e;
            }
            .pq-metric-row strong { color: #f0f2f5; }

            /* Full-width audit banner */
            .pq-banner-play {
                background: linear-gradient(90deg, rgba(63,185,80,0.35), rgba(35,134,54,0.15));
                border: 2px solid #3fb950;
                border-radius: 12px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.45rem;
                font-weight: 900;
                color: #3fb950;
                margin-top: 1rem;
                letter-spacing: 0.04em;
            }
            .pq-banner-pass {
                background: rgba(88,28,28,0.35);
                border: 2px solid #6e3630;
                border-radius: 12px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.35rem;
                font-weight: 900;
                color: #8b949e;
                margin-top: 1rem;
            }

            /* Hype vs Reality */
            .pq-hype-col {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 12px;
                padding: 1rem;
                text-align: center;
            }
            .pq-hype-val {
                font-size: 2rem;
                font-weight: 900;
                color: #f0f2f5;
            }
            .pq-bubble-badge {
                background: linear-gradient(90deg, rgba(255,140,0,0.35), rgba(255,69,0,0.2));
                border: 2px solid #ff8c00;
                color: #ffb347;
                font-weight: 900;
                font-size: 0.95rem;
                padding: 1rem 1.1rem;
                border-radius: 12px;
                text-align: center;
                margin-top: 0.85rem;
                box-shadow: 0 0 20px rgba(255,140,0,0.2);
            }

            /* Arb recipe */
            .pq-recipe {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 1rem 1.15rem;
                margin: 0.5rem 0;
            }
            .pq-recipe-step {
                font-size: 0.92rem;
                color: #c9d1d9;
                margin: 0.45rem 0;
                line-height: 1.5;
            }
            .pq-recipe-step strong { color: #58a6ff; }
            .pq-lock-banner {
                background: linear-gradient(90deg, rgba(63,185,80,0.3), rgba(35,134,54,0.12));
                border: 2px solid #3fb950;
                border-radius: 12px;
                padding: 1rem;
                text-align: center;
                font-size: 1.1rem;
                font-weight: 800;
                color: #3fb950;
                margin-top: 0.75rem;
            }

            /* Cross-book arb comparison */
            .pq-arb-compare {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 1rem 1.1rem;
                margin: 0.75rem 0 1rem;
            }
            .pq-arb-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 0.65rem;
            }
            @media (max-width: 640px) {
                .pq-arb-grid { grid-template-columns: 1fr; }
            }
            .pq-book-card {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 0.85rem;
            }
            .pq-book-header {
                font-size: 0.68rem;
                font-weight: 800;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.35rem;
            }
            .pq-book-title {
                font-size: 0.82rem;
                font-weight: 700;
                color: #f0f2f5;
                line-height: 1.35;
                margin-bottom: 0.65rem;
                min-height: 2.2rem;
            }
            .pq-odd-row {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 0.45rem 0.55rem;
                border-radius: 8px;
                margin-bottom: 0.35rem;
                font-size: 0.8rem;
                font-weight: 700;
            }
            .pq-odd-row.yes {
                background: rgba(88,166,255,0.12);
                border: 1px solid rgba(88,166,255,0.35);
                color: #58a6ff;
            }
            .pq-odd-row.no {
                background: #21262d;
                border: 1px solid #30363d;
                color: #c9d1d9;
            }
            .pq-odd-row .pq-odd-val {
                font-weight: 800;
                color: #f0f2f5;
                font-size: 0.78rem;
            }
            .pq-strategy-card {
                background: #161b22;
                border: 1px solid #21262d;
                border-radius: 14px;
                padding: 1rem 1.1rem;
                margin: 0.65rem 0;
            }
            .pq-strategy-card.pq-strategy-live {
                border-color: #3fb950;
                box-shadow: 0 0 20px rgba(63,185,80,0.15);
            }
            .pq-strategy-head {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 0.65rem;
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .pq-strategy-title {
                font-size: 0.95rem;
                font-weight: 800;
                color: #f0f2f5;
                margin: 0;
            }
            .pq-strategy-badge {
                font-size: 0.68rem;
                font-weight: 800;
                padding: 0.25rem 0.55rem;
                border-radius: 999px;
                text-transform: uppercase;
                letter-spacing: 0.04em;
            }
            .pq-strategy-badge.live {
                background: rgba(63,185,80,0.22);
                color: #3fb950;
                border: 1px solid #3fb950;
            }
            .pq-strategy-badge.dead {
                background: #21262d;
                color: #8b949e;
                border: 1px solid #30363d;
            }
            .pq-strategy-metrics {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 0.45rem;
                margin-top: 0.65rem;
            }
            @media (max-width: 480px) {
                .pq-strategy-metrics { grid-template-columns: 1fr; }
            }
            .pq-metric-box {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 10px;
                padding: 0.55rem 0.65rem;
                text-align: center;
            }
            .pq-metric-box .lbl {
                display: block;
                font-size: 0.62rem;
                font-weight: 700;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .pq-metric-box .val {
                display: block;
                font-size: 0.95rem;
                font-weight: 800;
                color: #f0f2f5;
                margin-top: 0.15rem;
            }
            .pq-metric-box .val.green { color: #3fb950; }
            .pq-metric-box .val.red { color: #f85149; }
            .pq-strategy-detail-grid {
                display: grid;
                grid-template-columns: repeat(2, minmax(0, 1fr));
                gap: 0.45rem;
                margin-top: 0.55rem;
            }
            @media (max-width: 720px) {
                .pq-strategy-detail-grid { grid-template-columns: 1fr; }
            }
            .pq-detail-box {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 10px;
                padding: 0.62rem 0.72rem;
            }
            .pq-detail-title {
                font-size: 0.68rem;
                font-weight: 800;
                color: #8b949e;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin: 0 0 0.38rem;
            }
            .pq-detail-line {
                font-size: 0.79rem;
                color: #c9d1d9;
                margin: 0.2rem 0;
            }
            .pq-detail-line .num {
                font-weight: 800;
                color: #f0f2f5;
            }
            .pq-detail-line .num.green { color: #3fb950; }
            .pq-detail-line .num.red { color: #f85149; }

            /* Kalshi auto-suggest */
            .pq-suggest-card {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 0.7rem 0.85rem;
                margin-bottom: 0.35rem;
            }
            .pq-suggest-score {
                display: inline-block;
                font-size: 0.65rem;
                font-weight: 800;
                color: #58a6ff;
                background: rgba(88,166,255,0.12);
                border: 1px solid rgba(88,166,255,0.35);
                border-radius: 999px;
                padding: 0.15rem 0.45rem;
                margin-bottom: 0.35rem;
            }
            .pq-suggest-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 700;
                color: #f0f2f5;
                line-height: 1.35;
            }
            .pq-suggest-meta {
                display: block;
                font-size: 0.72rem;
                color: #58a6ff;
                font-weight: 600;
                margin-top: 0.2rem;
            }
            .pq-build-tag {
                color: #58a6ff;
                font-weight: 700;
            }

            /* Pikkit-style performance calendar */
            .pq-perf-calendar {
                background: #0E121A;
                border: 1px solid #1F2937;
                border-radius: 10px;
                padding: 0.85rem 0.95rem 1rem;
                margin: 0.65rem 0 1rem;
            }
            .pq-perf-cal-header {
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                margin-bottom: 0.65rem;
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .pq-perf-cal-title {
                font-size: 0.95rem;
                font-weight: 800;
                color: #f0f2f5;
                letter-spacing: -0.02em;
            }
            .pq-perf-cal-sub {
                font-size: 0.72rem;
                font-weight: 600;
                color: #8b949e;
            }
            .pq-perf-cal-month-pnl {
                font-size: 0.82rem;
                font-weight: 800;
            }
            .pq-perf-cal-month-pnl.pos { color: #3fb950; }
            .pq-perf-cal-month-pnl.neg { color: #f85149; }
            .pq-perf-cal-month-pnl.flat { color: #8b949e; }
            .pq-perf-cal-grid {
                display: grid;
                grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 6px;
            }
            .pq-perf-cal-head {
                text-align: center;
                font-size: 0.62rem;
                font-weight: 800;
                color: #6e7681;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                padding: 0.2rem 0 0.35rem;
            }
            .pq-perf-cal-cell {
                min-height: 58px;
                border-radius: 8px;
                border: 1px solid #1F2937;
                background: #0A0C10;
                padding: 0.35rem 0.3rem 0.3rem;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                align-items: stretch;
            }
            .pq-perf-cal-cell.pq-perf-empty {
                background: transparent;
                border-color: transparent;
                min-height: 0;
                padding: 0;
            }
            .pq-perf-cal-cell.pq-perf-today {
                box-shadow: 0 0 0 2px #58a6ff;
            }
            .pq-perf-cal-cell.pq-perf-win {
                background: rgba(63,185,80,0.18);
                border-color: rgba(63,185,80,0.45);
            }
            .pq-perf-cal-cell.pq-perf-loss {
                background: rgba(248,81,73,0.14);
                border-color: rgba(248,81,73,0.4);
            }
            .pq-perf-cal-cell.pq-perf-flat {
                background: #161b22;
                border-color: #30363d;
            }
            .pq-perf-cal-day {
                font-size: 0.62rem;
                font-weight: 700;
                color: #8b949e;
                line-height: 1;
            }
            .pq-perf-cal-pnl {
                font-size: 0.72rem;
                font-weight: 800;
                text-align: center;
                line-height: 1.1;
                margin-top: 0.15rem;
            }
            .pq-perf-cal-pnl.pos { color: #3fb950; }
            .pq-perf-cal-pnl.neg { color: #f85149; }
            .pq-perf-cal-pnl.flat { color: #c9d1d9; }
            .pq-perf-cal-count {
                font-size: 0.58rem;
                font-weight: 600;
                color: #6e7681;
                text-align: center;
                margin-top: 0.1rem;
            }

            /* Ledger calendar flexbox (legacy) */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
            }
            .pq-cal-head {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                text-align: center;
                font-size: 0.65rem;
                font-weight: 700;
                color: #8b949e;
                padding: 0.25rem 0;
            }
            .pq-cal-cell {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                aspect-ratio: 1;
                border-radius: 8px;
                border: 1px solid #21262d;
                position: relative;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .pq-cal-day {
                position: absolute;
                top: 4px;
                left: 6px;
                font-size: 0.62rem;
                color: #8b949e;
                font-weight: 600;
            }
            .pq-cal-neutral { background: #161b22; }
            .pq-cal-win { background: rgba(63,185,80,0.22); border-color: rgba(63,185,80,0.4); }
            .pq-cal-loss { background: rgba(248,81,73,0.18); border-color: rgba(248,81,73,0.35); }
            .pq-cal-pnl { font-size: 0.72rem; font-weight: 800; }
            .pq-cal-pnl.pos { color: #3fb950; }
            .pq-cal-pnl.neg { color: #f85149; }
            .pq-cal-dash { color: #484f58; font-size: 0.85rem; }


            /* Elite value plays (SOP) */
            .pq-value-card-elite {
                border: 2px solid #3fb950;
                box-shadow: 0 0 22px rgba(63,185,80,0.28);
            }
            .pq-rank-badge {
                display: inline-block; background: rgba(63,185,80,0.2);
                color: #3fb950; border: 1px solid #3fb950;
                font-weight: 800; font-size: 0.72rem;
                padding: 0.28rem 0.6rem; border-radius: 999px;
                margin-bottom: 0.5rem; letter-spacing: 0.03em;
            }
            .pq-rank-badge-elite {
                background: rgba(63,185,80,0.35); font-size: 0.78rem;
            }

            /* Compact explore feed — single row on mobile */
            .pq-feed-compact {
                display: flex; align-items: center; justify-content: space-between;
                gap: 0.65rem; flex-wrap: wrap;
            }
            .pq-feed-body { flex: 1 1 200px; min-width: 0; }
            .pq-feed-odds {
                display: flex; gap: 0.35rem; flex-shrink: 0;
            }
            .pq-odd-pill.sm {
                padding: 0.35rem 0.5rem; font-size: 0.78rem;
                border-radius: 8px; white-space: nowrap;
            }

            /* Scrollable tabs on mobile */
            .stTabs [data-baseweb="tab-list"] {
                flex-wrap: nowrap !important;
                overflow-x: auto !important;
                -webkit-overflow-scrolling: touch;
            }
            .stTabs [data-baseweb="tab"] {
                white-space: nowrap !important;
                flex-shrink: 0 !important;
            }

            .block-container { max-width: 1100px; padding-bottom: 2rem; }

        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_value_play_card(row: pd.Series, rank: int) -> None:
    """Tactile standalone market card (presentation only)."""
    no_p = float(row["No Price"])
    model_win = float(row["Model Win %"])
    net_edge = float(row["Net EV Edge %"])
    implied_pct = no_p * 100.0
    card_cls = "pq-value-card pq-value-card-elite" if rank == 1 else "pq-value-card pq-value-card-hot"
    rank_cls = "pq-rank-badge pq-rank-badge-elite" if rank == 1 else "pq-rank-badge"
    rank_label = "Best Play #1" if rank == 1 else f"Edge #{rank}"
    event = html.escape(str(row["Question"]))
    st.markdown(
        f"""
        <div class="{card_cls}">
            <span class="{rank_cls}">{rank_label}</span>
            <p class="pq-event-name">{event}</p>
            <div class="pq-cta-pill">BET NO AT ${no_p:.2f}</div>
            <div class="pq-metric-row">
                <span class="pq-ev-badge">+{net_edge:.2f}% NET EV</span>
                <span>True Prob <strong>{model_win:.1f}%</strong></span>
                <span>Market Implied <strong>{implied_pct:.1f}%</strong></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_value_plays_dataframe(df: pd.DataFrame) -> None:
    """Presentation-only terminal table for elite value plays."""
    odds_fmt = get_odds_format()
    implied_pct = (df["No Price"].astype(float) * 100.0).round(1)
    true_prob = df["Model Win %"].astype(float).round(1)
    quant_edge = df["Net EV Edge %"].astype(float).round(1)

    table = pd.DataFrame(
        {
            "Rank": list(range(1, len(df) + 1)),
            "Market": df["Question"].astype(str).values,
            "Market Line": [
                format_odds_display(float(p), odds_fmt) for p in df["No Price"]
            ],
            "Implied %": implied_pct.values,
            "True Prob": true_prob.values,
            "Quant Edge": [f"+{e:.1f}%" for e in quant_edge],
            "NO ¢": df["Cost ¢"].astype(float).round(1).values,
            "Volume": df["Volume"].astype(float).round(0).values,
        }
    )

    row_h = 38
    table_h = min(row_h * len(table) + row_h, 320)

    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        height=table_h,
        column_config={
            "Rank": st.column_config.NumberColumn(
                "#",
                width="small",
                format="%d",
            ),
            "Market": st.column_config.TextColumn(
                "Market",
                width="large",
                help="Polymarket contract — bet NO for the edge",
            ),
            "Market Line": st.column_config.TextColumn(
                "Market Line",
                width="medium",
                help="Implied NO odds in your selected format",
            ),
            "Implied %": st.column_config.ProgressColumn(
                "Implied",
                format="%.1f%%",
                min_value=0,
                max_value=100,
                width="medium",
            ),
            "True Prob": st.column_config.ProgressColumn(
                "True Prob",
                format="%.1f%%",
                min_value=0,
                max_value=100,
                width="medium",
            ),
            "Quant Edge": st.column_config.TextColumn(
                "Quant Edge",
                width="small",
                help="Net EV edge after platform fees",
            ),
            "NO ¢": st.column_config.NumberColumn(
                "NO ¢",
                format="%.1f",
                width="small",
            ),
            "Volume": st.column_config.NumberColumn(
                "Volume",
                format="$%,.0f",
                width="small",
            ),
        },
    )


def render_top_value_plays() -> None:
    st.markdown("### 🔥 Top Value Plays")
    st.caption(
        f"Elite tier — win prob >{VALUE_PLAYS_WIN_MIN:.0f}%, net EV ≥{VALUE_PLAYS_EV_EDGE_MIN:.0f}% · "
        f"top {VALUE_PLAYS_MAX} sharpest edges"
    )

    if st.button("↻ Refresh markets", key="refresh_poly"):
        fetch_polymarket_markets.clear()
        st.rerun()

    try:
        raw_df = fetch_polymarket_markets()
    except Exception:
        st.error("Markets unavailable — try refreshing.")
        return

    if raw_df.empty:
        st.warning("No active markets found.")
        return

    search = st.session_state.get("global_search_query", "")
    df = _filter_value_plays(raw_df)
    if search.strip():
        df = df[df["Question"].str.contains(search.strip(), case=False, na=False)].copy()

    if df.empty:
        st.markdown(
            """
            <div class="pq-value-card" style="text-align:center;padding:2rem 1.25rem;">
                <p class="pq-event-name" style="margin-bottom:0.5rem;">No action today</p>
                <p style="color:#8b949e;font-size:0.95rem;line-height:1.5;margin:0;">
                    No mathematically viable anomalies detected. Maintain bankroll discipline.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    st.caption(f"{len(df)} elite anomal{'y' if len(df) == 1 else 'ies'} on slate")
    _render_value_plays_dataframe(df)


def _kelly_allocation_pct(true_win_prob: float, share_price_cents: float) -> float:
    """Presentation-only Kelly fraction (%) for a $1-settlement contract at offer price."""
    p = true_win_prob / 100.0
    c = share_price_cents / 100.0
    if not (0.0 < c < 1.0):
        return 0.0
    kelly = (p - c) / (1.0 - c)
    return max(0.0, min(kelly, 1.0)) * 100.0


def _audit_rationale_text(
    true_win_prob: float,
    stake: float,
    share_price: float,
    ev_dollars: float,
    ev_yield_pct: float,
    kelly_pct: float,
    prob_ok: bool,
    ev_ok: bool,
) -> str:
    """Narrative deep-dive for audit expander (display only)."""
    market_implied = share_price
    return f"""
**Model inputs**
- Your true win estimate: **{true_win_prob:.1f}%**
- Offered share price: **{share_price:.1f}¢** (market implied **{market_implied:.1f}%**)
- Stake: **${stake:,.2f}**

**Settlement math** (unchanged engine)
- Projected EV: **${ev_dollars:+,.2f}** on ${stake:,.2f} stake
- Quantitative edge: **{ev_yield_pct:+.2f}%**
- Kelly allocation (presentation): **{kelly_pct:.1f}%** of bankroll units

**Gatekeeper thresholds**
- Win probability gate (≥ {WIN_PROB_THRESHOLD:.0f}%): **{"PASS" if prob_ok else "FAIL"}**
- EV edge gate (≥ {EV_THRESHOLD:.1f}%): **{"PASS" if ev_ok else "FAIL"}**

**Desk read**
{"Both gates clear — size within Kelly discipline and execute if liquidity supports the line." if prob_ok and ev_ok else "One or more gates failed — reduce size or pass until the line moves."}
"""


def _render_audit_results(
    true_win_prob: float,
    stake: float,
    share_price: float,
    ev_dollars: float,
    ev_yield_pct: float,
) -> None:
    """Structured metric layout for Audit My Bet readout."""
    kelly_pct = _kelly_allocation_pct(true_win_prob, share_price)
    prob_ok = true_win_prob >= WIN_PROB_THRESHOLD
    ev_ok = ev_yield_pct >= EV_THRESHOLD
    edge_display = f"+{ev_yield_pct:.2f}%" if ev_yield_pct >= 0 else f"{ev_yield_pct:.2f}%"

    m1, m2, m3 = st.columns(3)
    m1.metric("📊 True Probability", f"{true_win_prob:.1f}%")
    m2.metric("⚡ Quantitative Edge", edge_display)
    m3.metric("💰 Recommended Allocation", f"{kelly_pct:.1f}% units")

    if ev_yield_pct >= 5.0:
        st.success(
            "✅ MATURED VALUE ADVANTAGE: Line clears quantitative model thresholds."
        )
    elif ev_yield_pct > 0.0:
        st.warning(
            "⚠️ MARGINAL EDGE: Positive EV but below the 5% matured-advantage band — "
            "size down or wait for a better line."
        )
    else:
        st.error(
            "⛔ NO EDGE: Offer price exceeds model fair value — pass and preserve bankroll."
        )

    rationale = _audit_rationale_text(
        true_win_prob,
        stake,
        share_price,
        ev_dollars,
        ev_yield_pct,
        kelly_pct,
        prob_ok,
        ev_ok,
    )
    with st.expander("🔍 View Full Model Rationale & Scraped Context", expanded=False):
        st.markdown(rationale)


def render_audit_my_bet() -> None:
    st.markdown("### ⚖️ Audit My Bet")

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        true_win_prob = st.number_input(
            "Your win estimate (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
        )
    with col2:
        share_price = st.number_input(
            "Share price (¢)",
            min_value=0.01,
            max_value=99.99,
            value=50.0,
            step=1.0,
        )
    c1, c2, c3 = st.columns(3)
    with c1:
        stake = st.number_input(
            "Stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    ev_dollars, ev_yield_pct = _calc_ev_dollars(true_win_prob, stake, share_price)
    _render_audit_results(true_win_prob, stake, share_price, ev_dollars, ev_yield_pct)


def render_hype_vs_reality() -> None:
    st.markdown("### 📣 Hype vs. Reality")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<p style="color:#8b949e;font-size:0.75rem;margin:0 0 0.25rem;">What people are saying</p>', unsafe_allow_html=True)
        sentiment = st.slider("Social Sentiment", 0.0, 100.0, 50.0, 0.5, label_visibility="collapsed", key="hype_sent")
        st.markdown(f'<p class="pq-hype-val">{sentiment:.0f}%</p>', unsafe_allow_html=True)
    with col2:
        st.markdown('<p style="color:#8b949e;font-size:0.75rem;margin:0 0 0.25rem;">What the math says</p>', unsafe_allow_html=True)
        implied_prob = st.slider("True Win", 0.0, 100.0, 50.0, 0.5, label_visibility="collapsed", key="hype_real")
        st.markdown(f'<p class="pq-hype-val">{implied_prob:.0f}%</p>', unsafe_allow_html=True)

    delta = sentiment - implied_prob
    if delta >= DIVERGENCE_TRIGGER:
        st.markdown(
            '<div class="pq-bubble-badge">Narrative bubble — consider fading the public</div>',
            unsafe_allow_html=True,
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.markdown(
            '<div class="pq-card"><span class="pq-badge pq-badge-blue">Crowd too bearish — YES may be cheap</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="pq-card"><span class="pq-badge pq-badge-grey">Aligned — no narrative edge right now</span></div>',
            unsafe_allow_html=True,
        )


def render_trap_detector() -> None:
    """Alias for legacy reference."""
    render_hype_vs_reality()


def render_global_search_bar() -> str:
    """Pikkit-style persistent search (synced across all views)."""
    st.markdown('<div class="pq-search-hero">', unsafe_allow_html=True)
    query = st.text_input(
        "Search markets",
        key="global_search_query",
        placeholder="Search teams, players, events…",
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)
    return (query or "").strip().lower()


def _filter_explore_catalog(
    catalog: pd.DataFrame,
    query: str,
    category: str,
    sports_type: str,
    source: str,
) -> pd.DataFrame:
    df = catalog.copy()
    if source != "Both":
        df = df[df["Source"] == source]
    if category == "Player Props":
        df = df[df["Category"] == "Player Props"]
    elif category != "All":
        df = df[df["Category"] == category]
    if category in ("Sports", "Player Props") and sports_type != "All":
        if category != "Player Props":
            df = df[df["Subcategory"] == sports_type]
    if query:
        df = df[df["Search Blob"].str.contains(query, na=False, regex=False)]
    return df.reset_index(drop=True)


def _sync_selection_from_catalog(row: pd.Series) -> None:
    """Push explore selection into arb pickers."""
    if row["Source"] == "Polymarket":
        st.session_state.poly_selected = row["id"]
        st.session_state.arb_poly_anchor = None
    else:
        st.session_state.kalshi_selected = row["id"]


def _render_matchup_feed(page_df: pd.DataFrame, odds_fmt: str) -> None:
    """Compact scannable rows — market info + odds inline, one tap to select."""
    for idx, row in page_df.iterrows():
        yes_odds = format_odds_display(float(row["Yes Price"]), odds_fmt)
        no_odds = format_odds_display(float(row["No Price"]), odds_fmt)
        event_line = ""
        if row.get("Event Title") and str(row["Event Title"]).strip():
            event_line = (
                f'<span class="pq-feed-event">{html.escape(str(row["Event Title"]))}</span>'
            )
        st.markdown(
            f"""
            <div class="pq-feed-row pq-feed-compact">
                <div class="pq-feed-body">
                    <span class="pq-feed-meta">{html.escape(str(row["Source"]))} ·
                    {html.escape(str(row["Category"]))} · {html.escape(str(row["Subcategory"]))}</span>
                    <span class="pq-feed-title">{html.escape(str(row["Title"]))}</span>
                    {event_line}
                </div>
                <div class="pq-feed-odds">
                    <span class="pq-odd-pill pq-odd-yes sm">YES {html.escape(yes_odds)}</span>
                    <span class="pq-odd-pill pq-odd-no sm">NO {html.escape(no_odds)}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Select market →", key=f"explore_pick_{row['Catalog ID']}_{idx}", use_container_width=True):
            _sync_selection_from_catalog(row)
            st.session_state.explore_last_pick = str(row["Title"])
            st.rerun()


def render_explore_hub() -> None:
    st.markdown("### 🔍 Explore")
    query = st.session_state.get("global_search_query", "").strip().lower()

    try:
        catalog = build_explore_catalog()
    except Exception:
        st.error("Could not load the market catalog. Try refreshing.")
        return

    if catalog.empty:
        st.warning("No markets available to explore right now.")
        return

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        category = st.selectbox(
            "Category",
            options=list(EXPLORE_CATEGORIES),
            key="explore_category",
            label_visibility="visible",
        )
    with col2:
        source = st.selectbox(
            "Source",
            options=list(EXPLORE_SOURCES),
            key="explore_source",
            label_visibility="visible",
        )
    c1, c2, c3 = st.columns(3)
    sports_type = "All"
    with c1:
        if category in ("Sports", "Player Props"):
            sports_type = st.selectbox(
                "Market Type",
                options=list(EXPLORE_SPORTS_TYPES),
                key="explore_sports_type",
                label_visibility="visible",
            )
    with c2:
        if st.button("↻ Refresh catalog", key="refresh_explore", use_container_width=True):
            build_explore_catalog.clear()
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            fetch_kalshi_player_props.clear()
            st.rerun()
    with c3:
        st.caption("Tap **Select** on a row to load into Arbs.")
    st.markdown("</div>", unsafe_allow_html=True)

    filtered = _filter_explore_catalog(
        catalog,
        query,
        category or "All",
        sports_type or "All",
        source or "Both",
    )

    if filtered.empty:
        st.info("No markets match your filters. Try a different search or category.")
        return

    odds_fmt = get_odds_format()
    total_pages = max(1, (len(filtered) + EXPLORE_PAGE_SIZE - 1) // EXPLORE_PAGE_SIZE)
    page = min(st.session_state.explore_page, total_pages - 1)
    st.session_state.explore_page = page
    start = page * EXPLORE_PAGE_SIZE
    page_df = filtered.iloc[start : start + EXPLORE_PAGE_SIZE]

    st.caption(f"{len(filtered)} markets · showing {start + 1}–{start + len(page_df)}")

    if st.session_state.get("explore_last_pick"):
        st.success(f"Selected: {st.session_state.explore_last_pick}")

    _render_matchup_feed(page_df, odds_fmt)

    n1, n2, n3 = st.columns([1, 2, 1])
    with n1:
        if st.button("← Prev", key="explore_prev", disabled=page == 0):
            st.session_state.explore_page = page - 1
            st.rerun()
    with n2:
        st.markdown(
            f'<p class="pq-page-indicator">Page {page + 1} / {total_pages}</p>',
            unsafe_allow_html=True,
        )
    with n3:
        if st.button("Next →", key="explore_next", disabled=page >= total_pages - 1):
            st.session_state.explore_page = page + 1
            st.rerun()

    st.markdown("#### Quick Actions")
    qa1, qa2 = st.columns(2)
    with qa1:
        if st.button("⚖️ Audit Selected Bet", use_container_width=True):
            st.session_state.explore_action_hint = "Switch to the **⚖️ Audit My Bet** tab to run the math."
            st.rerun()
    with qa2:
        if st.button("💰 Cross-Book Arb", use_container_width=True):
            st.session_state.explore_action_hint = (
                "Switch to the **💰 Risk-Free Arbs** tab — your pick is pre-loaded."
            )
            st.rerun()

    if st.session_state.get("explore_action_hint"):
        st.info(st.session_state.explore_action_hint)


def _render_cross_book_odds(
    poly_row: pd.Series,
    kalshi_row: pd.Series,
    odds_fmt: str,
) -> None:
    """Side-by-side Polymarket vs Kalshi YES/NO prices."""
    poly_yes = float(poly_row["Yes Price"])
    poly_no = float(poly_row["No Price"])
    kalshi_yes = float(kalshi_row["Kalshi YES Cost"])
    kalshi_no = float(kalshi_row["Kalshi NO Cost"])

    def _row(side: str, price: float) -> str:
        cents = price * 100.0
        odds = format_odds_display(price, odds_fmt)
        cls = "yes" if side == "YES" else "no"
        return (
            f'<div class="pq-odd-row {cls}">'
            f"<span>{side}</span>"
            f'<span class="pq-odd-val">{cents:.1f}¢ · {html.escape(odds)}</span>'
            f"</div>"
        )

    poly_title = html.escape(_select_label(str(poly_row["Question"]), 80))
    kalshi_title = html.escape(_select_label(str(kalshi_row["Title"]), 80))

    st.markdown(
        f"""
        <div class="pq-arb-compare">
            <p class="pq-section-label" style="margin-top:0;">Cross-book odds comparison</p>
            <div class="pq-arb-grid">
                <div class="pq-book-card">
                    <div class="pq-book-header">Polymarket</div>
                    <div class="pq-book-title">{poly_title}</div>
                    {_row("YES", poly_yes)}
                    {_row("NO", poly_no)}
                </div>
                <div class="pq-book-card">
                    <div class="pq-book-header">Kalshi</div>
                    <div class="pq-book-title">{kalshi_title}</div>
                    {_row("YES", kalshi_yes)}
                    {_row("NO", kalshi_no)}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_arb_strategy_card(
    label: str,
    poly_side: str,
    poly_price: float,
    kalshi_side: str,
    kalshi_price: float,
    stake: float,
    odds_fmt: str,
) -> None:
    """One arb recipe with legs, combined cost, ROI, and profit at stake."""
    total_cost = poly_price + kalshi_price
    net, roi = _arb_opportunity(total_cost)
    is_arb = total_cost < 1.0
    total_outlay = stake * 2.0

    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    poly_c = poly_price * 100.0
    kalshi_c = kalshi_price * 100.0
    poly_shares_equal = (stake / poly_price) if poly_price > 0 else 0.0
    kalshi_shares_equal = (stake / kalshi_price) if kalshi_price > 0 else 0.0
    poly_win_net_equal = poly_shares_equal - total_outlay
    kalshi_win_net_equal = kalshi_shares_equal - total_outlay
    equal_floor = min(poly_win_net_equal, kalshi_win_net_equal)
    equal_ceiling = max(poly_win_net_equal, kalshi_win_net_equal)

    hedged_shares = (total_outlay / total_cost) if total_cost > 0 else 0.0
    hedged_poly_stake = hedged_shares * poly_price
    hedged_kalshi_stake = hedged_shares * kalshi_price
    hedged_profit = hedged_shares - total_outlay

    card_cls = "pq-strategy-card pq-strategy-live" if is_arb else "pq-strategy-card"
    badge_cls = "pq-strategy-badge live" if is_arb else "pq-strategy-badge dead"
    badge_txt = "🔒 Arb locked" if is_arb else "No lock"

    lock_html = ""
    if is_arb:
        lock_html = (
            f'<div class="pq-lock-banner">Guaranteed +${hedged_profit:,.2f} profit on '
            f"${total_outlay:,.0f} total outlay</div>"
        )

    floor_cls = "green" if equal_floor >= 0 else "red"
    ceil_cls = "green" if equal_ceiling >= 0 else "red"
    hedge_cls = "green" if hedged_profit >= 0 else "red"
    poly_outcome_lbl = f"If {poly_side} resolves"
    kalshi_outcome_lbl = f"If {kalshi_side} resolves"

    st.markdown(
        f"""
        <div class="{card_cls}">
            <div class="pq-strategy-head">
                <p class="pq-strategy-title">{html.escape(label)}</p>
                <span class="{badge_cls}">{badge_txt}</span>
            </div>
            <div class="pq-split">
                <div class="pq-split-side">
                    <div class="venue">Polymarket</div>
                    <div class="leg">Buy {html.escape(poly_side)}</div>
                    <div style="font-size:0.78rem;color:#8b949e;margin-top:0.35rem;">
                        {poly_c:.1f}¢ · {html.escape(poly_odds)} · ${stake:,.0f}
                        = {poly_shares_equal:,.2f} shares
                    </div>
                </div>
                <div class="pq-split-side">
                    <div class="venue">Kalshi</div>
                    <div class="leg">Buy {html.escape(kalshi_side)}</div>
                    <div style="font-size:0.78rem;color:#8b949e;margin-top:0.35rem;">
                        {kalshi_c:.1f}¢ · {html.escape(kalshi_odds)} · ${stake:,.0f}
                        = {kalshi_shares_equal:,.2f} shares
                    </div>
                </div>
            </div>
            <div class="pq-strategy-metrics">
                <div class="pq-metric-box">
                    <span class="lbl">Combined cost</span>
                    <span class="val">{total_cost * 100:.1f}¢ / $1</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">ROI</span>
                    <span class="val {'green' if is_arb else ''}">{roi:+.2f}%</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">Hedged lock</span>
                    <span class="val {hedge_cls}">${hedged_profit:+,.2f}</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">Net edge</span>
                    <span class="val {'green' if is_arb else ''}">${net:.4f}/$1</span>
                </div>
            </div>
            <div class="pq-strategy-detail-grid">
                <div class="pq-detail-box">
                    <p class="pq-detail-title">Recommended hedge sizing (locks both outcomes)</p>
                    <p class="pq-detail-line">Polymarket {html.escape(poly_side)} stake:
                        <span class="num">${hedged_poly_stake:,.2f}</span></p>
                    <p class="pq-detail-line">Kalshi {html.escape(kalshi_side)} stake:
                        <span class="num">${hedged_kalshi_stake:,.2f}</span></p>
                    <p class="pq-detail-line">Buy
                        <span class="num">{hedged_shares:,.2f}</span> shares on each side</p>
                    <p class="pq-detail-line">Guaranteed net:
                        <span class="num {hedge_cls}">${hedged_profit:+,.2f}</span></p>
                </div>
                <div class="pq-detail-box">
                    <p class="pq-detail-title">Equal ${stake:,.0f}/leg outcome range</p>
                    <p class="pq-detail-line">{html.escape(poly_outcome_lbl)}:
                        <span class="num {'green' if poly_win_net_equal >= 0 else 'red'}">
                        ${poly_win_net_equal:+,.2f}</span></p>
                    <p class="pq-detail-line">{html.escape(kalshi_outcome_lbl)}:
                        <span class="num {'green' if kalshi_win_net_equal >= 0 else 'red'}">
                        ${kalshi_win_net_equal:+,.2f}</span></p>
                    <p class="pq-detail-line">Worst case:
                        <span class="num {floor_cls}">${equal_floor:+,.2f}</span></p>
                    <p class="pq-detail-line">Best case:
                        <span class="num {ceil_cls}">${equal_ceiling:+,.2f}</span></p>
                </div>
            </div>
        </div>
        {lock_html}
        """,
        unsafe_allow_html=True,
    )


def _render_arb_split(
    poly_side: str,
    poly_price: float,
    kalshi_side: str,
    kalshi_price: float,
    net: float,
    roi: float,
    is_arb: bool,
    odds_fmt: str,
) -> None:
    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    banner = ""
    if is_arb:
        banner = f"""
        <div class="pq-arb-banner">
            <h3>💰 Guaranteed Profit Locked In</h3>
            <p><strong>${net:.4f}</strong> net return per $1 settled
            &nbsp;·&nbsp; <strong>{roi:.2f}% ROI</strong></p>
        </div>
        """

    st.markdown(
        f"""
        <div class="pq-split">
            <div class="pq-split-side">
                <div class="venue">Polymarket</div>
                <div class="leg">Buy {poly_side} @ {poly_odds}</div>
            </div>
            <div class="pq-split-side">
                <div class="venue">Kalshi</div>
                <div class="leg">Buy {kalshi_side} @ {kalshi_odds}</div>
            </div>
        </div>
        {banner}
        """,
        unsafe_allow_html=True,
        )


def _render_arb_recipe(
    label: str,
    step1: str,
    step2: str,
    net: float,
    stake: float,
    is_arb: bool,
) -> None:
    profit = net * stake
    lock = ""
    if is_arb:
        lock = f'<div class="pq-lock-banner">Guaranteed Lock: +${profit:.2f} Profit</div>'
    st.markdown(
        f"""
        <div class="pq-recipe">
            <p style="font-weight:800;color:#f0f2f5;margin:0 0 0.5rem;">{html.escape(label)}</p>
            <p class="pq-recipe-step"><strong>Step 1:</strong> {html.escape(step1)}</p>
            <p class="pq-recipe-step"><strong>Step 2:</strong> {html.escape(step2)}</p>
        </div>
        {lock}
        """,
        unsafe_allow_html=True,
    )


def render_risk_free_arbs() -> None:
    st.markdown("### 💰 Risk-Free Arbs")
    st.caption(
        "Pick one market on each exchange · we compare YES/NO, show exact hedge sizing,"
        " and break down P/L by outcome."
    )

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        arb_stake = st.number_input(
            "Stake per leg ($)",
            min_value=1.0,
            value=DEFAULT_ARB_STAKE,
            step=10.0,
            key="arb_stake",
        )
    with c2:
        if st.button("↻ Refresh prices", key="refresh_arb", use_container_width=True):
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            fetch_kalshi_player_props.clear()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    try:
        poly_df = fetch_polymarket_markets()
        kalshi_main = fetch_kalshi_markets()
        kalshi_props = fetch_kalshi_player_props()
        kalshi_df = pd.concat([kalshi_main, kalshi_props], ignore_index=True).drop_duplicates(
            subset=["ticker"]
        )
    except Exception:
        st.error("Unable to load exchange prices.")
        return

    poly_priced = poly_df.dropna(subset=["Yes Price", "No Price"]).copy()
    kalshi_priced = _filter_kalshi_tradeable(kalshi_df)

    if poly_priced.empty or kalshi_priced.empty:
        st.warning("Not enough priced contracts on both books.")
        return

    odds_fmt = get_odds_format()
    poly_options = {row["id"]: row["Question"] for _, row in poly_priced.iterrows()}
    poly_prices = {
        row["id"]: f"YES {format_odds_display(float(row['Yes Price']), odds_fmt)} · "
        f"NO {format_odds_display(float(row['No Price']), odds_fmt)}"
        for _, row in poly_priced.iterrows()
    }
    kalshi_options = {row["ticker"]: row["Title"] for _, row in kalshi_priced.iterrows()}
    kalshi_prices = {
        row["ticker"]: f"YES {format_odds_display(float(row['Kalshi YES Cost']), odds_fmt)} · "
        f"NO {format_odds_display(float(row['Kalshi NO Cost']), odds_fmt)}"
        for _, row in kalshi_priced.iterrows()
    }

    poly_id = render_searchable_picker(
        "Polymarket Event", poly_options, "poly_selected", show_prices=poly_prices,
    )
    if not poly_id:
        return

    poly_title = poly_options[poly_id]
    suggestions = _sync_kalshi_auto_suggest(poly_id, poly_title, kalshi_priced)
    _render_kalshi_suggestions(suggestions, kalshi_prices)

    kalshi_ticker = render_searchable_picker(
        "Kalshi Event", kalshi_options, "kalshi_selected", show_prices=kalshi_prices,
    )
    if not kalshi_ticker:
        return

    poly_row = poly_priced.loc[poly_priced["id"] == poly_id].iloc[0]
    kalshi_row = kalshi_priced.loc[kalshi_priced["ticker"] == kalshi_ticker].iloc[0]

    poly_yes = float(poly_row["Yes Price"])
    poly_no = float(poly_row["No Price"])
    kalshi_yes = float(kalshi_row["Kalshi YES Cost"])
    kalshi_no = float(kalshi_row["Kalshi NO Cost"])

    _render_cross_book_odds(poly_row, kalshi_row, odds_fmt)

    st.markdown('<p class="pq-section-label">Arb strategies</p>', unsafe_allow_html=True)

    _render_arb_strategy_card(
        "Strategy A — Poly YES + Kalshi NO",
        "YES", poly_yes,
        "NO", kalshi_no,
        arb_stake,
        odds_fmt,
    )
    _render_arb_strategy_card(
        "Strategy B — Poly NO + Kalshi YES",
        "NO", poly_no,
        "YES", kalshi_yes,
        arb_stake,
        odds_fmt,
    )

    cost_a = poly_yes + kalshi_no
    cost_b = poly_no + kalshi_yes
    if cost_a >= 1.0 and cost_b >= 1.0:
        st.markdown(
            '<div class="pq-card"><span class="pq-badge pq-badge-grey">'
            "No guaranteed lock on this pair — combined costs exceed $1.00 on both recipes."
            "</span></div>",
            unsafe_allow_html=True,
        )


def _build_performance_calendar_html(
    daily_perf: dict[date, float],
    daily_counts: dict[date, int],
    year: int,
    month: int,
    today: date,
) -> str:
    """Pikkit-style month grid with daily net P&L tiles."""
    weekday, num_days = calendar.monthrange(year, month)
    heads = "".join(
        f'<div class="pq-perf-cal-head">{html.escape(d)}</div>'
        for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    )

    month_total = 0.0
    cells: list[str] = []
    for _ in range(weekday):
        cells.append('<div class="pq-perf-cal-cell pq-perf-empty"></div>')

    for day in range(1, num_days + 1):
        d = date(year, month, day)
        pnl = daily_perf.get(d)
        count = daily_counts.get(d, 0)
        today_cls = " pq-perf-today" if d == today else ""

        if pnl is None:
            cells.append(
                f'<div class="pq-perf-cal-cell{today_cls}">'
                f'<span class="pq-perf-cal-day">{day}</span></div>'
            )
        elif pnl > 0:
            month_total += pnl
            count_line = (
                f'<span class="pq-perf-cal-count">{count} bet{"s" if count != 1 else ""}</span>'
                if count else ""
            )
            cells.append(
                f'<div class="pq-perf-cal-cell pq-perf-win{today_cls}">'
                f'<span class="pq-perf-cal-day">{day}</span>'
                f'<span class="pq-perf-cal-pnl pos">+${pnl:,.0f}</span>{count_line}</div>'
            )
        elif pnl < 0:
            month_total += pnl
            count_line = (
                f'<span class="pq-perf-cal-count">{count} bet{"s" if count != 1 else ""}</span>'
                if count else ""
            )
            cells.append(
                f'<div class="pq-perf-cal-cell pq-perf-loss{today_cls}">'
                f'<span class="pq-perf-cal-day">{day}</span>'
                f'<span class="pq-perf-cal-pnl neg">-${abs(pnl):,.0f}</span>{count_line}</div>'
            )
        else:
            count_line = (
                f'<span class="pq-perf-cal-count">{count} bet{"s" if count != 1 else ""}</span>'
                if count else ""
            )
            cells.append(
                f'<div class="pq-perf-cal-cell pq-perf-flat{today_cls}">'
                f'<span class="pq-perf-cal-day">{day}</span>'
                f'<span class="pq-perf-cal-pnl flat">$0</span>{count_line}</div>'
            )

    month_cls = "pos" if month_total > 0 else "neg" if month_total < 0 else "flat"
    month_lbl = f"${month_total:+,.0f}" if month_total else "$0"
    month_name = datetime(year, month, 1).strftime("%B %Y")
    grid = "".join(cells)

    return f"""
    <div class="pq-perf-calendar">
        <div class="pq-perf-cal-header">
            <div>
                <div class="pq-perf-cal-title">{html.escape(month_name)}</div>
                <div class="pq-perf-cal-sub">Daily settled P&amp;L</div>
            </div>
            <div class="pq-perf-cal-month-pnl {month_cls}">Month: {month_lbl}</div>
        </div>
        <div class="pq-perf-cal-grid">{heads}{grid}</div>
    </div>
    """


def _render_performance_calendar(ledger: pd.DataFrame) -> None:
    """Performance Ledger calendar panel (presentation only)."""
    daily_perf = _aggregate_daily_performance(ledger)
    daily_counts = _ledger_daily_bet_counts(ledger)
    today = datetime.now(timezone.utc).date()

    if "ledger_cal_year" not in st.session_state:
        st.session_state.ledger_cal_year = today.year
        st.session_state.ledger_cal_month = today.month

    year = int(st.session_state.ledger_cal_year)
    month = int(st.session_state.ledger_cal_month)

    st.markdown('<p class="pq-section-label">Performance calendar</p>', unsafe_allow_html=True)
    nav_l, nav_m, nav_r = st.columns([1, 2, 1])
    with nav_l:
        if st.button("←", key="ledger_cal_prev"):
            if month == 1:
                st.session_state.ledger_cal_month = 12
                st.session_state.ledger_cal_year = year - 1
            else:
                st.session_state.ledger_cal_month = month - 1
            st.rerun()
    with nav_m:
        st.markdown(
            f'<p class="pq-page-indicator">{datetime(year, month, 1).strftime("%B %Y")}</p>',
            unsafe_allow_html=True,
        )
    with nav_r:
        if st.button("→", key="ledger_cal_next"):
            if month == 12:
                st.session_state.ledger_cal_month = 1
                st.session_state.ledger_cal_year = year + 1
            else:
                st.session_state.ledger_cal_month = month + 1
            st.rerun()

    st.markdown(
        _build_performance_calendar_html(daily_perf, daily_counts, year, month, today),
        unsafe_allow_html=True,
    )


def _render_api_keys_setup_panel(creds: dict[str, bool]) -> None:
    """In-app guide with direct links to obtain Kalshi + Polymarket API credentials."""
    missing_k = not creds["kalshi"]
    missing_p = not creds["polymarket"]

    if missing_k or missing_p:
        st.markdown("#### 🔑 Connect your accounts")
        if missing_k and missing_p:
            st.caption("Add at least one platform to start syncing fills into The Ledger.")
        elif missing_k:
            st.success("Polymarket connected ✓ — add Kalshi below to sync both books.")
        else:
            st.success("Kalshi connected ✓ — add Polymarket below to sync both books.")

    with st.expander(
        "Where do I get API keys?",
        expanded=missing_k or missing_p,
    ):
        k1, k2 = st.columns(2)

        with k1:
            st.markdown("**Kalshi**")
            if creds["kalshi"]:
                st.caption("✓ Credentials detected")
            else:
                st.markdown(
                    f"1. Open **[Kalshi → Account → API Keys]({KALSHI_KEYS_PAGE})**\n"
                    "2. Click **Create New API Key**\n"
                    "3. Save your **Key ID** → `KALSHI_API_KEY_ID`\n"
                    "4. Save the **private key** (PEM) → `KALSHI_PRIVATE_KEY`\n\n"
                    f"📖 [Kalshi API key docs]({KALSHI_KEYS_DOCS})"
                )

        with k2:
            st.markdown("**Polymarket**")
            if creds["polymarket"]:
                st.caption("✓ Credentials detected")
            else:
                st.markdown(
                    "1. Sign in to Polymarket with your wallet\n"
                    "2. Derive **L2 API credentials** (apiKey, secret, passphrase)\n"
                    f"   → [Authentication guide]({POLYMARKET_AUTH_DOCS})\n"
                    "3. Add to `.env`:\n"
                    "   - `POLYMARKET_API_KEY`\n"
                    "   - `POLYMARKET_API_SECRET`\n"
                    "   - `POLYMARKET_API_PASSPHRASE`\n"
                    "   - `POLYMARKET_WALLET_ADDRESS` (optional)\n\n"
                    f"📖 [Trading & API overview]({POLYMARKET_TRADING_DOCS})"
                )

        st.markdown("---")
        st.markdown(
            "**Local run:** create a `.env` file in the project root (already git-ignored).\n\n"
            f"**Streamlit Cloud:** paste the same variables under "
            f"**App settings → Secrets** — "
            f"[Streamlit secrets docs]({STREAMLIT_SECRETS_DOCS})"
        )

        st.code(
            """# .env example
KALSHI_API_KEY_ID=your_kalshi_key_id
KALSHI_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\\n...\\n-----END RSA PRIVATE KEY-----"

POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_base64_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
POLYMARKET_WALLET_ADDRESS=0xYourPolygonAddress""",
            language="bash",
        )


def render_ledger() -> None:
    st.markdown("### 📒 The Ledger")

    creds = _ledger_credentials()
    _render_api_keys_setup_panel(creds)

    if not creds["kalshi"] and not creds["polymarket"]:
        st.info("Connect at least one account, then tap **Sync Fills** to populate your ledger.")

    if st.button("Sync fills", key="refresh_ledger", type="primary"):
        fetch_unified_ledger.clear()
        st.rerun()

    ledger = fetch_unified_ledger()
    daily_net, wl_record, capital_at_risk = _ledger_kpis(ledger)

    k1, k2, k3 = st.columns(3)
    daily_cls = "pq-ev-badge" if daily_net >= 0 else "pq-badge-red"
    daily_lbl = f"${daily_net:+,.2f}"
    k1.markdown(
        f'<div class="pq-card"><p class="pq-section-label">Daily Net Profit</p>'
        f'<p style="font-size:1.4rem;font-weight:900;margin:0;"><span class="{daily_cls}">{daily_lbl}</span></p></div>',
        unsafe_allow_html=True,
    )
    k2.metric("Monthly W/L Record", wl_record)
    k3.metric("Total Capital at Risk", f"${capital_at_risk:,.2f}")

    _render_performance_calendar(ledger)

    if ledger.empty:
        st.caption("No filled orders ingested yet.")
        return

    display = ledger[
        ["Date", "Platform Badge", "Event Name", "Position Taken", "Stake $", "Price Paid ¢", "Status", "Net Return $"]
    ].copy()

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=360,
        column_config={
            "Date": st.column_config.TextColumn("Date", width=90),
            "Platform Badge": st.column_config.TextColumn("Platform", width=60),
            "Event Name": st.column_config.TextColumn("Event Name", width=220),
            "Position Taken": st.column_config.TextColumn("Position", width=80),
            "Stake $": st.column_config.NumberColumn("Stake $", format="$%.2f"),
            "Price Paid ¢": st.column_config.NumberColumn("Price ¢", format="%.1f"),
            "Status": st.column_config.TextColumn("Status", width=70),
            "Net Return $": st.column_config.NumberColumn("Net Return $", format="$%.2f"),
        },
    )


# --------------------------------------------------------------------------- #
# App shell
# --------------------------------------------------------------------------- #

st.set_page_config(
    page_title=f"POLY-QUANT · {APP_BUILD}",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
    <style>
    /* Minimize top padding of the main container */
    .block-container {
        padding-top: 1rem !important;
        padding-bottom: 1rem !important;
        max-width: 95% !important;
    }
    /* Hide the default Streamlit header bar decoration */
    header {visibility: hidden;}
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* ── Quant terminal theme ── */
    .stApp {
        background-color: #050505 !important;
        color: #e5e7eb !important;
    }
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div {
        background-color: #0A0C10 !important;
    }

    /* Stylized container tiles */
    [data-testid="stMetricContainer"] {
        background-color: #0E121A !important;
        border: 1px solid #1F2937 !important;
        border-radius: 6px !important;
        padding: 10px 15px !important;
    }
    div.stExpander {
        background-color: #0A0C10 !important;
        border: 1px solid #1F2937 !important;
        border-radius: 6px !important;
    }
    </style>
    """, unsafe_allow_html=True)

_inject_global_css()
_init_session()


def _render_deploy_strip() -> None:
    """Always-visible deploy fingerprint so Cloud vs local is obvious."""
    st.markdown(
        f"""
        <div style="background:#0d2818;border:1px solid #3fb950;border-radius:6px;
        padding:0.4rem 0.7rem;margin-bottom:0.45rem;font-size:0.76rem;color:#8b949e;">
        <span style="color:#3fb950;font-weight:800;">LIVE BUILD</span>
        &nbsp;{html.escape(APP_BUILD)}&nbsp;·&nbsp;commit&nbsp;
        <code style="color:#58a6ff;">{html.escape(GIT_SHA)}</code>
        &nbsp;·&nbsp;Ledger calendar + dataframe terminal active
        </div>
        """,
        unsafe_allow_html=True,
    )


_render_deploy_strip()

st.markdown(
    f"""
    <div class="pq-topbar">
        <span class="pq-topbar-brand">POLY-QUANT</span>
        <span class="pq-topbar-meta">Polymarket · Kalshi ·
        <span class="pq-build-tag">Build {html.escape(APP_BUILD)}</span></span>
    </div>
    """,
    unsafe_allow_html=True,
)

tool_l, tool_r = st.columns([3, 1])
with tool_l:
    render_global_search_bar()
with tool_r:
    render_odds_format_toggle()


def main() -> None:
    (
        tab_plays,
        tab_explore,
        tab_audit,
        tab_hype,
        tab_arb,
        tab_ledger,
    ) = st.tabs(
        [
            "🔥 Value Plays",
            "🔍 Explore",
            "⚖️ Check Bet",
            "📣 Sentiment",
            "💰 Arbs",
            "📒 Ledger",
        ]
    )

    with tab_plays:
        render_top_value_plays()

    with tab_explore:
        render_explore_hub()

    with tab_audit:
        render_audit_my_bet()

    with tab_hype:
        render_hype_vs_reality()

    with tab_arb:
        render_risk_free_arbs()

    with tab_ledger:
        render_ledger()


main()
