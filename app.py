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
APP_BUILD = "4.0.0-modern-ui"
GIT_SHA = "ui-overhaul+"

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


def _signed_money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _pct_label(price: Optional[float]) -> str:
    if price is None or pd.isna(price):
        return "--%"
    return f"{price * 100:.1f}%"


def _book_price_hint(
    book: str,
    yes_price: Optional[float],
    no_price: Optional[float],
    odds_fmt: str,
) -> str:
    """Compact picker metadata showing the book and YES/NO implied percentages."""
    yes_odds = format_odds_display(yes_price, odds_fmt)
    no_odds = format_odds_display(no_price, odds_fmt)
    return (
        f"{book} · YES {_pct_label(yes_price)} ({yes_odds}) · "
        f"NO {_pct_label(no_price)} ({no_odds})"
    )


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
                st.session_state.kalshi_selected_picker_open = False
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
            st.session_state.kalshi_selected_picker_open = False
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
    collapse_after_select: bool = False,
) -> Optional[str]:
    """
  Mobile-friendly market picker: search → paginated tap-to-select cards.
  Replaces native selectbox long-list UX.
    """
    if not options:
        st.warning(f"No {label} markets available.")
        return None

    ids = list(options.keys())
    open_key = f"{session_key}_picker_open"
    if open_key not in st.session_state:
        st.session_state[open_key] = True

    if st.session_state.get(session_key) not in options:
        st.session_state[session_key] = ids[0]
        st.session_state[open_key] = True

    page_key = f"{session_key}_page"
    if page_key not in st.session_state:
        st.session_state[page_key] = 0

    st.markdown(f'<p class="pq-section-label">{label}</p>', unsafe_allow_html=True)
    sel_id = st.session_state.get(session_key)
    if collapse_after_select and sel_id in options and not st.session_state.get(open_key, True):
        selected_price = ""
        if show_prices and sel_id in show_prices:
            selected_price = (
                f'<span class="pq-pick-meta">{html.escape(show_prices[sel_id])}</span>'
            )
        st.markdown(
            f'<div class="pq-selected-banner"><strong>Selected:</strong> '
            f'{html.escape(options.get(sel_id, ""))}{selected_price}</div>',
            unsafe_allow_html=True,
        )
        if st.button(f"Change {label}", key=f"{session_key}_change", use_container_width=True):
            st.session_state[open_key] = True
            st.rerun()
        return sel_id

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
            if collapse_after_select:
                st.session_state[open_key] = False
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
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap');

            :root {
                --pq-bg:           #07080d;
                --pq-bg-soft:      #0b0d14;
                --pq-surface-1:    #11141d;
                --pq-surface-2:    #161a25;
                --pq-surface-3:    #1c2030;
                --pq-border:       #232838;
                --pq-border-strong:#2e3447;
                --pq-text:         #f8fafc;
                --pq-text-2:       #cbd5e1;
                --pq-muted:        #94a3b8;
                --pq-faint:        #64748b;
                --pq-accent:       #818cf8;
                --pq-accent-2:     #a78bfa;
                --pq-cyan:         #22d3ee;
                --pq-success:      #34d399;
                --pq-success-2:    #10b981;
                --pq-warning:      #fbbf24;
                --pq-danger:       #f87171;
                --pq-shadow-soft:  0 1px 0 rgba(255,255,255,0.04) inset,
                                   0 12px 32px -16px rgba(0,0,0,0.6);
                --pq-shadow-pop:   0 1px 0 rgba(255,255,255,0.06) inset,
                                   0 18px 48px -18px rgba(99,102,241,0.35);
                --pq-radius-sm:    8px;
                --pq-radius:       12px;
                --pq-radius-lg:    16px;
                --pq-radius-xl:    20px;
            }

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            html, body, .stApp {
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                font-feature-settings: 'cv11','ss01','ss03','tnum';
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
            }

            .stApp {
                background: var(--pq-bg);
                background-image:
                    radial-gradient(ellipse 90% 60% at 8% -10%,  rgba(99,102,241,0.22), transparent 60%),
                    radial-gradient(ellipse 70% 50% at 95% 0%,   rgba(34,211,238,0.14),  transparent 65%),
                    radial-gradient(ellipse 80% 40% at 50% 110%, rgba(167,139,250,0.12), transparent 70%);
                background-attachment: fixed;
                color: var(--pq-text);
            }

            .block-container {
                padding: 0.65rem 1rem 2rem;
                max-width: 1180px;
            }

            ::selection { background: rgba(129,140,248,0.35); color: #fff; }

            ::-webkit-scrollbar { width: 10px; height: 10px; }
            ::-webkit-scrollbar-track { background: transparent; }
            ::-webkit-scrollbar-thumb {
                background: var(--pq-border);
                border-radius: 999px;
                border: 2px solid transparent;
                background-clip: padding-box;
            }
            ::-webkit-scrollbar-thumb:hover { background: var(--pq-border-strong); background-clip: padding-box; }

            a, a:visited { color: var(--pq-accent); text-decoration: none; }
            a:hover { color: #c7d2fe; text-decoration: underline; }
            code, kbd, samp { font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, monospace; }

            /* ── App header ─────────────────────────────────────────── */
            .pq-topbar {
                position: relative;
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 0.5rem 1rem;
                padding: 0.85rem 1.1rem;
                margin: 0 0 0.8rem;
                background:
                    linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01)) ,
                    linear-gradient(135deg, rgba(99,102,241,0.10) 0%, rgba(34,211,238,0.06) 100%),
                    var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                box-shadow: var(--pq-shadow-soft);
                overflow: hidden;
            }
            .pq-topbar::before {
                content: "";
                position: absolute; inset: 0 0 auto 0; height: 1px;
                background: linear-gradient(90deg, transparent, rgba(167,139,250,0.6), transparent);
            }
            .pq-topbar-brand {
                display: inline-flex;
                align-items: center;
                gap: 0.65rem;
                font-size: 1.05rem;
                font-weight: 900;
                letter-spacing: -0.02em;
                color: #ffffff;
            }
            .pq-brand-mark {
                width: 30px; height: 30px;
                display: inline-flex; align-items: center; justify-content: center;
                border-radius: 9px;
                background: linear-gradient(135deg, #6366f1 0%, #a78bfa 60%, #22d3ee 120%);
                color: #fff; font-weight: 900; font-size: 0.95rem;
                box-shadow: 0 8px 22px -8px rgba(99,102,241,0.7),
                            inset 0 1px 0 rgba(255,255,255,0.25);
            }
            .pq-brand-name { letter-spacing: -0.02em; }
            .pq-brand-tag {
                font-size: 0.66rem; font-weight: 700; letter-spacing: 0.18em;
                color: var(--pq-accent); text-transform: uppercase;
                background: rgba(129,140,248,0.10);
                border: 1px solid rgba(129,140,248,0.30);
                padding: 0.18rem 0.45rem; border-radius: 999px;
            }
            .pq-topbar-meta {
                display: inline-flex; align-items: center; gap: 0.6rem;
                font-size: 0.72rem; color: var(--pq-muted); font-weight: 500;
            }
            .pq-topbar-meta .dot {
                width: 7px; height: 7px; border-radius: 999px;
                background: var(--pq-success);
                box-shadow: 0 0 0 4px rgba(52,211,153,0.15);
                display: inline-block;
                animation: pq-pulse 2.4s ease-in-out infinite;
            }
            @keyframes pq-pulse {
                0%, 100% { opacity: 1; transform: scale(1); }
                50%      { opacity: 0.55; transform: scale(1.25); }
            }

            /* Legacy hero — kept for compatibility */
            .pq-hero {
                background: linear-gradient(135deg, var(--pq-surface-1) 0%, var(--pq-surface-2) 100%);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 0.85rem 1.1rem;
                margin-bottom: 0.65rem;
            }
            .pq-hero h1 {
                margin: 0; font-size: 1.15rem; font-weight: 900;
                letter-spacing: -0.02em; color: var(--pq-text);
            }
            .pq-hero p {
                margin: 0.2rem 0 0; font-size: 0.78rem;
                color: var(--pq-muted); font-weight: 500;
            }

            /* ── Status / build strip ───────────────────────────────── */
            .pq-status-strip {
                display: flex; align-items: center; gap: 0.6rem;
                padding: 0.45rem 0.75rem;
                margin: 0 0 0.75rem;
                background: linear-gradient(90deg, rgba(52,211,153,0.10), rgba(129,140,248,0.06));
                border: 1px solid rgba(52,211,153,0.30);
                border-radius: 999px;
                font-size: 0.72rem; color: var(--pq-text-2);
                flex-wrap: wrap;
            }
            .pq-status-strip .pq-status-live {
                font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
                color: var(--pq-success);
            }
            .pq-status-strip code {
                color: var(--pq-accent); font-size: 0.72rem;
                background: rgba(129,140,248,0.10);
                padding: 0.05rem 0.4rem; border-radius: 6px;
                border: 1px solid rgba(129,140,248,0.25);
            }
            .pq-status-strip .pq-status-dot {
                width: 7px; height: 7px; border-radius: 999px;
                background: var(--pq-success);
                box-shadow: 0 0 0 4px rgba(52,211,153,0.18);
                animation: pq-pulse 2.4s ease-in-out infinite;
            }

            /* ── Market Pulse KPI strip ─────────────────────────────── */
            .pq-pulse-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 0.6rem;
                margin: 0.25rem 0 1rem;
            }
            .pq-pulse-tile {
                position: relative;
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.75rem 0.9rem;
                box-shadow: var(--pq-shadow-soft);
                overflow: hidden;
                transition: transform .18s ease, border-color .18s ease;
            }
            .pq-pulse-tile:hover { transform: translateY(-1px); border-color: var(--pq-border-strong); }
            .pq-pulse-tile::before {
                content: "";
                position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
                background: linear-gradient(180deg, var(--pq-accent), var(--pq-accent-2));
                opacity: 0.85;
            }
            .pq-pulse-tile.accent-emerald::before { background: linear-gradient(180deg, #34d399, #10b981); }
            .pq-pulse-tile.accent-cyan::before    { background: linear-gradient(180deg, #22d3ee, #06b6d4); }
            .pq-pulse-tile.accent-amber::before   { background: linear-gradient(180deg, #fbbf24, #f59e0b); }
            .pq-pulse-tile.accent-rose::before    { background: linear-gradient(180deg, #fb7185, #f43f5e); }
            .pq-pulse-label {
                font-size: 0.62rem; font-weight: 800;
                letter-spacing: 0.10em; text-transform: uppercase;
                color: var(--pq-muted);
                display: flex; align-items: center; gap: 0.35rem;
            }
            .pq-pulse-value {
                font-size: 1.4rem; font-weight: 900;
                color: var(--pq-text); letter-spacing: -0.02em;
                margin: 0.18rem 0 0.1rem; line-height: 1.1;
                font-variant-numeric: tabular-nums;
            }
            .pq-pulse-value.pos { color: var(--pq-success); }
            .pq-pulse-value.neg { color: var(--pq-danger); }
            .pq-pulse-sub  {
                font-size: 0.7rem; color: var(--pq-faint); font-weight: 600;
            }

            /* ── Onboarding / how-it-works ──────────────────────────── */
            .pq-onboard {
                background: linear-gradient(135deg, rgba(99,102,241,0.10), rgba(34,211,238,0.05));
                border: 1px solid rgba(129,140,248,0.30);
                border-radius: var(--pq-radius-lg);
                padding: 0.9rem 1rem;
                margin: 0.1rem 0 1rem;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-onboard-title {
                font-size: 0.85rem; font-weight: 800;
                color: var(--pq-text); margin: 0 0 0.55rem;
                display: flex; align-items: center; gap: 0.45rem;
                letter-spacing: -0.01em;
            }
            .pq-onboard-steps {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 0.5rem;
            }
            .pq-onboard-step {
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.6rem 0.7rem;
                display: flex; gap: 0.55rem; align-items: flex-start;
            }
            .pq-onboard-step .num {
                width: 24px; height: 24px; border-radius: 999px;
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-accent-2));
                color: #fff; font-weight: 900; font-size: 0.75rem;
                display: inline-flex; align-items: center; justify-content: center;
                flex-shrink: 0;
                box-shadow: 0 6px 18px -8px rgba(99,102,241,0.6);
            }
            .pq-onboard-step .body { line-height: 1.4; }
            .pq-onboard-step .body strong {
                color: var(--pq-text); font-size: 0.82rem; display: block;
                margin-bottom: 0.15rem;
            }
            .pq-onboard-step .body span {
                color: var(--pq-muted); font-size: 0.74rem;
            }

            /* ── Tab header ─────────────────────────────────────────── */
            .pq-tab-head {
                display: flex; justify-content: space-between; align-items: center;
                gap: 0.65rem; flex-wrap: wrap;
                margin: 0.35rem 0 0.45rem;
            }
            .pq-tab-head h2 {
                font-size: 1.15rem; font-weight: 900; letter-spacing: -0.02em;
                color: var(--pq-text); margin: 0;
                display: inline-flex; align-items: center; gap: 0.45rem;
            }
            .pq-tab-head .pq-tab-sub {
                color: var(--pq-muted); font-size: 0.78rem; margin: 0.05rem 0 0;
                font-weight: 500;
            }
            .pq-tab-hint {
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-left: 3px solid var(--pq-accent);
                border-radius: var(--pq-radius);
                padding: 0.55rem 0.75rem;
                margin: 0 0 0.85rem;
                font-size: 0.78rem;
                color: var(--pq-text-2);
                line-height: 1.45;
            }
            .pq-tab-hint strong { color: var(--pq-text); }

            /* Tabs */
            .stTabs [data-baseweb="tab-list"] {
                gap: 4px;
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 4px;
                box-shadow: var(--pq-shadow-soft);
            }
            .stTabs [data-baseweb="tab"] {
                background: transparent;
                color: var(--pq-muted);
                font-weight: 700;
                font-size: 0.82rem;
                padding: 9px 14px;
                border-radius: 9px;
                border: none;
                transition: color .15s ease, background .15s ease, transform .15s ease;
            }
            .stTabs [data-baseweb="tab"]:hover {
                color: var(--pq-text); background: rgba(255,255,255,0.025);
            }
            .stTabs [aria-selected="true"] {
                color: #ffffff !important;
                background: linear-gradient(135deg, rgba(99,102,241,0.55), rgba(167,139,250,0.45)) !important;
                box-shadow: 0 6px 18px -8px rgba(99,102,241,0.55),
                            inset 0 1px 0 rgba(255,255,255,0.18) !important;
            }
            .stTabs [data-baseweb="tab-highlight"] { display: none !important; }
            .stTabs [data-baseweb="tab-border"] { display: none !important; }

            /* Cards */
            .pq-card {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 0.9rem 1.05rem;
                margin-bottom: 0.6rem;
                box-shadow: var(--pq-shadow-soft);
                transition: border-color .18s ease, transform .18s ease;
            }
            .pq-card:hover { border-color: var(--pq-border-strong); }
            .pq-card-compound {
                border-color: rgba(52,211,153,0.45);
                background: linear-gradient(135deg, rgba(52,211,153,0.18) 0%, var(--pq-surface-2) 70%);
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 18px 40px -20px rgba(16,185,129,0.45);
            }
            .pq-card-title {
                font-size: 0.92rem; font-weight: 800; color: var(--pq-text);
                line-height: 1.35; margin: 0 0 0.55rem;
                letter-spacing: -0.01em;
            }
            .pq-card-row {
                display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center;
            }

            /* Badges */
            .pq-badge {
                display: inline-flex; align-items: center; gap: 0.3rem;
                padding: 0.24rem 0.6rem;
                border-radius: 999px;
                font-size: 0.7rem;
                font-weight: 800;
                letter-spacing: 0.02em;
                white-space: nowrap;
            }
            .pq-badge-green {
                background: rgba(52,211,153,0.14);
                color: var(--pq-success);
                border: 1px solid rgba(52,211,153,0.42);
            }
            .pq-badge-blue {
                background: rgba(129,140,248,0.14);
                color: var(--pq-accent);
                border: 1px solid rgba(129,140,248,0.40);
            }
            .pq-badge-grey {
                background: rgba(148,163,184,0.10);
                color: var(--pq-muted);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-badge-red {
                background: rgba(248,113,113,0.12);
                color: var(--pq-danger);
                border: 1px solid rgba(248,113,113,0.40);
            }
            .pq-stat {
                font-size: 0.78rem; color: var(--pq-muted);
                font-variant-numeric: tabular-nums;
            }
            .pq-stat strong { color: var(--pq-text); font-weight: 800; }

            /* Verdict containers */
            .pq-verdict-play {
                position: relative;
                background:
                    radial-gradient(ellipse 80% 60% at 10% 0%, rgba(52,211,153,0.25), transparent 70%),
                    linear-gradient(135deg, rgba(52,211,153,0.16) 0%, rgba(16,185,129,0.06) 100%),
                    var(--pq-surface-1);
                border: 1px solid rgba(52,211,153,0.55);
                border-radius: var(--pq-radius-lg);
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
                box-shadow: 0 0 0 1px rgba(52,211,153,0.12) inset,
                            0 20px 50px -22px rgba(16,185,129,0.55);
            }
            .pq-verdict-play h2 {
                margin: 0 0 0.35rem; font-size: 1.35rem; font-weight: 900;
                color: var(--pq-success); letter-spacing: -0.02em;
            }
            .pq-verdict-play p {
                margin: 0; font-size: 0.95rem; color: var(--pq-text-2); line-height: 1.5;
            }
            .pq-verdict-pass {
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
            }
            .pq-verdict-pass h2 {
                margin: 0 0 0.35rem; font-size: 1.2rem; font-weight: 900;
                color: var(--pq-muted);
            }
            .pq-verdict-pass p {
                margin: 0; font-size: 0.88rem; color: var(--pq-faint);
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
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.95rem;
                text-align: center;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-split-side .venue {
                font-size: 0.66rem;
                font-weight: 800;
                color: var(--pq-muted);
                text-transform: uppercase;
                letter-spacing: 0.10em;
                margin-bottom: 0.35rem;
            }
            .pq-split-side .leg {
                font-size: 1.05rem;
                font-weight: 900;
                color: var(--pq-accent);
                letter-spacing: -0.01em;
            }
            .pq-arb-banner {
                background:
                    radial-gradient(ellipse 80% 70% at 0% 50%, rgba(52,211,153,0.30), transparent 70%),
                    linear-gradient(90deg, rgba(52,211,153,0.20), rgba(16,185,129,0.05));
                border: 1px solid rgba(52,211,153,0.55);
                border-radius: var(--pq-radius);
                padding: 1rem 1.1rem;
                text-align: center;
                margin-top: 0.65rem;
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 18px 42px -22px rgba(16,185,129,0.55);
            }
            .pq-arb-banner h3 {
                margin: 0 0 0.25rem; color: var(--pq-success);
                font-size: 1.05rem; font-weight: 900; letter-spacing: -0.02em;
            }
            .pq-arb-banner p { margin: 0; color: var(--pq-text-2); font-size: 0.9rem; }

            /* Warning banner */
            .pq-trap-banner {
                background:
                    radial-gradient(ellipse 80% 60% at 100% 0%, rgba(248,113,113,0.22), transparent 70%),
                    linear-gradient(135deg, rgba(248,113,113,0.14), rgba(244,63,94,0.05));
                border: 1px solid rgba(248,113,113,0.55);
                border-radius: var(--pq-radius);
                padding: 1.1rem 1.2rem;
                margin-top: 0.75rem;
                box-shadow: 0 18px 42px -22px rgba(244,63,94,0.55);
            }
            .pq-trap-banner h3 {
                margin: 0 0 0.4rem; color: var(--pq-danger);
                font-size: 1rem; font-weight: 900; letter-spacing: -0.01em;
            }
            .pq-trap-banner p { margin: 0; color: var(--pq-text-2); font-size: 0.88rem; line-height: 1.45; }

            /* Input card */
            .pq-input-card {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 0.9rem 1.05rem 0.35rem;
                margin-bottom: 0.85rem;
                box-shadow: var(--pq-shadow-soft);
            }

            /* Streamlit widgets */
            [data-testid="stMetric"] {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.7rem 0.9rem;
                box-shadow: var(--pq-shadow-soft);
            }
            [data-testid="stMetric"] [data-testid="stMetricLabel"] {
                color: var(--pq-muted) !important;
                text-transform: uppercase;
                font-size: 0.66rem !important;
                font-weight: 800 !important;
                letter-spacing: 0.10em;
            }
            [data-testid="stMetric"] [data-testid="stMetricValue"] {
                font-variant-numeric: tabular-nums;
                font-weight: 900;
                letter-spacing: -0.02em;
                color: var(--pq-text) !important;
            }
            [data-testid="stMetricDelta"] svg { display: none; }
            [data-testid="stDataFrame"], [data-testid="stTable"] {
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                overflow: hidden;
                box-shadow: var(--pq-shadow-soft);
            }
            .stSlider label, .stNumberInput label, .stSelectbox label, .stTextInput label {
                font-weight: 700 !important;
                font-size: 0.78rem !important;
                color: var(--pq-text-2) !important;
                text-transform: uppercase;
                letter-spacing: 0.06em;
            }
            .stNumberInput input, .stTextInput input, .stTextArea textarea {
                background: var(--pq-surface-2) !important;
                color: var(--pq-text) !important;
                border: 1px solid var(--pq-border) !important;
                border-radius: 10px !important;
                font-variant-numeric: tabular-nums;
            }
            .stNumberInput input:focus, .stTextInput input:focus, .stTextArea textarea:focus {
                border-color: var(--pq-accent) !important;
                box-shadow: 0 0 0 3px rgba(129,140,248,0.20) !important;
            }
            .stSelectbox [data-baseweb="select"] > div {
                background: var(--pq-surface-2) !important;
                border-color: var(--pq-border) !important;
                border-radius: 10px !important;
            }
            .stSlider [data-baseweb="slider"] [role="slider"] {
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-accent-2)) !important;
                border: 0 !important;
                box-shadow: 0 4px 14px -4px rgba(129,140,248,0.5) !important;
            }
            .stSlider [data-baseweb="slider"] > div > div > div {
                background: linear-gradient(90deg, var(--pq-accent), var(--pq-accent-2)) !important;
            }
            hr { border-color: var(--pq-border); margin: 0.85rem 0; }

            /* Streamlit alert boxes */
            .stAlert {
                border-radius: var(--pq-radius) !important;
                border: 1px solid var(--pq-border) !important;
            }
            .stAlert[data-baseweb="notification"] {
                background: var(--pq-surface-1) !important;
            }

            /* Section labels & picker */
            .pq-section-label {
                font-size: 0.66rem;
                font-weight: 800;
                color: var(--pq-muted);
                text-transform: uppercase;
                letter-spacing: 0.12em;
                margin: 0.65rem 0 0.35rem;
            }
            .pq-pick-card {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: 10px;
                padding: 0.6rem 0.8rem;
                margin-bottom: 0.3rem;
                transition: border-color .15s ease, background .15s ease;
            }
            .pq-pick-card:hover {
                border-color: var(--pq-accent);
                background: rgba(129,140,248,0.06);
            }
            .pq-pick-selected {
                border-color: var(--pq-accent) !important;
                background: rgba(129,140,248,0.10) !important;
                box-shadow: 0 0 0 3px rgba(129,140,248,0.15);
            }
            .pq-pick-title {
                display: block; font-size: 0.84rem; font-weight: 700;
                color: var(--pq-text); line-height: 1.35;
            }
            .pq-pick-meta {
                display: block; font-size: 0.72rem;
                color: var(--pq-accent); font-weight: 700; margin-top: 0.15rem;
            }
            .pq-page-indicator {
                text-align: center; font-size: 0.78rem;
                color: var(--pq-text-2); margin: 0.35rem 0 0;
                font-weight: 700;
            }
            .pq-selected-banner {
                background: linear-gradient(90deg, rgba(129,140,248,0.10), rgba(34,211,238,0.05));
                border: 1px solid rgba(129,140,248,0.30);
                border-radius: var(--pq-radius);
                padding: 0.7rem 0.85rem;
                font-size: 0.8rem;
                color: var(--pq-text-2);
                line-height: 1.45;
                margin: 0.5rem 0 0.75rem;
            }
            .pq-odds-bar {
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.6rem 0.8rem 0.4rem;
                margin-bottom: 0.65rem;
            }

            /* Tactile buttons */
            .stButton > button, .stDownloadButton > button {
                border-radius: 10px !important;
                font-weight: 700 !important;
                font-size: 0.82rem !important;
                min-height: 2.4rem;
                background: var(--pq-surface-2) !important;
                border: 1px solid var(--pq-border) !important;
                color: var(--pq-text) !important;
                transition: transform .12s ease, border-color .15s ease, background .15s ease, box-shadow .15s ease !important;
            }
            .stButton > button:hover {
                border-color: var(--pq-accent) !important;
                background: rgba(129,140,248,0.10) !important;
                transform: translateY(-1px);
            }
            .stButton > button[kind="primary"] {
                background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%) !important;
                border: 1px solid rgba(255,255,255,0.10) !important;
                color: #fff !important;
                box-shadow: 0 10px 26px -10px rgba(99,102,241,0.65),
                            inset 0 1px 0 rgba(255,255,255,0.16) !important;
            }
            .stButton > button[kind="primary"]:hover {
                background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%) !important;
                box-shadow: 0 14px 32px -10px rgba(99,102,241,0.75) !important;
            }
            .stButton > button:focus-visible {
                outline: none !important;
                box-shadow: 0 0 0 3px rgba(129,140,248,0.35) !important;
            }

            /* Segmented control polish */
            [data-testid="stSegmentedControl"] {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: 10px;
                padding: 3px;
            }

            /* Pikkit-style explore feed */
            .pq-search-hero {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 0.6rem 0.85rem;
                margin-bottom: 0.55rem;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-feed-row {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.8rem 0.9rem;
                margin-bottom: 0.5rem;
                transition: border-color .15s ease, transform .15s ease;
            }
            .pq-feed-row:hover {
                border-color: var(--pq-border-strong);
                transform: translateY(-1px);
            }
            .pq-feed-meta {
                display: block;
                font-size: 0.62rem;
                font-weight: 800;
                color: var(--pq-muted);
                text-transform: uppercase;
                letter-spacing: 0.10em;
                margin-bottom: 0.25rem;
            }
            .pq-feed-title {
                display: block; font-size: 0.88rem; font-weight: 700;
                color: var(--pq-text); line-height: 1.35;
            }
            .pq-feed-event {
                display: block; font-size: 0.72rem; color: var(--pq-faint); margin-top: 0.2rem;
            }
            .pq-odd-pill {
                display: block; text-align: center;
                padding: 0.55rem 0.45rem;
                border-radius: 10px;
                font-weight: 900;
                font-size: 0.92rem;
                font-variant-numeric: tabular-nums;
                letter-spacing: -0.01em;
            }
            .pq-odd-yes {
                background: rgba(52,211,153,0.10);
                color: var(--pq-success);
                border: 1px solid rgba(52,211,153,0.35);
            }
            .pq-odd-no {
                background: rgba(248,113,113,0.08);
                color: var(--pq-danger);
                border: 1px solid rgba(248,113,113,0.30);
            }
            .pq-nav-scroll .stPills { overflow-x: auto; }

            /* Phase 1 — tactile value cards */
            .pq-value-card {
                position: relative;
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 1.05rem 1.15rem;
                margin-bottom: 0.7rem;
                box-shadow: var(--pq-shadow-soft);
                transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
            }
            .pq-value-card:hover {
                border-color: var(--pq-border-strong);
                transform: translateY(-1px);
            }
            .pq-value-card-hot {
                border-color: rgba(52,211,153,0.45);
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 18px 38px -22px rgba(16,185,129,0.45);
            }
            .pq-event-name {
                font-size: 0.98rem;
                font-weight: 800;
                color: var(--pq-text);
                margin: 0 0 0.7rem;
                line-height: 1.35;
                letter-spacing: -0.01em;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(135deg, #6366f1, #8b5cf6);
                color: #fff;
                font-weight: 900;
                font-size: 0.82rem;
                padding: 0.5rem 0.95rem;
                border-radius: 999px;
                margin-bottom: 0.6rem;
                letter-spacing: 0.02em;
                box-shadow: 0 10px 26px -10px rgba(99,102,241,0.65),
                            inset 0 1px 0 rgba(255,255,255,0.18);
            }
            .pq-ev-badge {
                display: inline-flex;
                align-items: center;
                gap: 0.3rem;
                background: rgba(52,211,153,0.14);
                color: var(--pq-success);
                border: 1px solid rgba(52,211,153,0.45);
                font-weight: 900;
                font-size: 0.8rem;
                padding: 0.32rem 0.7rem;
                border-radius: 999px;
                letter-spacing: 0.02em;
            }
            .pq-metric-row {
                display: flex; gap: 1.25rem; flex-wrap: wrap;
                font-size: 0.78rem; color: var(--pq-muted);
                font-variant-numeric: tabular-nums;
            }
            .pq-metric-row strong { color: var(--pq-text); font-weight: 800; }

            /* Full-width audit banner */
            .pq-banner-play {
                background:
                    radial-gradient(ellipse 80% 60% at 10% 0%, rgba(52,211,153,0.30), transparent 70%),
                    linear-gradient(90deg, rgba(52,211,153,0.22), rgba(16,185,129,0.05));
                border: 1px solid rgba(52,211,153,0.55);
                border-radius: var(--pq-radius);
                padding: 1.4rem;
                text-align: center;
                font-size: 1.4rem;
                font-weight: 900;
                color: var(--pq-success);
                margin-top: 1rem;
                letter-spacing: 0.02em;
                box-shadow: 0 22px 50px -22px rgba(16,185,129,0.55);
            }
            .pq-banner-pass {
                background: var(--pq-surface-1);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1.3rem;
                text-align: center;
                font-size: 1.3rem;
                font-weight: 900;
                color: var(--pq-muted);
                margin-top: 1rem;
            }

            /* Hype vs Reality */
            .pq-hype-col {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1rem;
                text-align: center;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-hype-val {
                font-size: 2.2rem; font-weight: 900;
                color: var(--pq-text); letter-spacing: -0.03em;
                font-variant-numeric: tabular-nums;
                background: linear-gradient(135deg, var(--pq-text) 0%, var(--pq-accent) 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            .pq-bubble-badge {
                background:
                    radial-gradient(ellipse 80% 60% at 0% 50%, rgba(251,191,36,0.30), transparent 70%),
                    linear-gradient(90deg, rgba(251,191,36,0.20), rgba(245,158,11,0.08));
                border: 1px solid rgba(251,191,36,0.55);
                color: var(--pq-warning);
                font-weight: 900;
                font-size: 0.95rem;
                padding: 1rem 1.1rem;
                border-radius: var(--pq-radius);
                text-align: center;
                margin-top: 0.85rem;
                box-shadow: 0 18px 42px -22px rgba(245,158,11,0.55);
            }

            /* Arb recipe */
            .pq-recipe {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 1rem 1.15rem;
                margin: 0.5rem 0;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-recipe-step {
                font-size: 0.92rem;
                color: var(--pq-text-2);
                margin: 0.45rem 0;
                line-height: 1.5;
            }
            .pq-recipe-step strong { color: var(--pq-accent); font-weight: 800; }
            .pq-lock-banner {
                background:
                    radial-gradient(ellipse 80% 60% at 0% 50%, rgba(52,211,153,0.30), transparent 70%),
                    linear-gradient(90deg, rgba(52,211,153,0.22), rgba(16,185,129,0.06));
                border: 1px solid rgba(52,211,153,0.55);
                border-radius: var(--pq-radius);
                padding: 1rem;
                text-align: center;
                font-size: 1.1rem;
                font-weight: 900;
                color: var(--pq-success);
                margin-top: 0.75rem;
                box-shadow: 0 18px 42px -22px rgba(16,185,129,0.55);
                letter-spacing: -0.01em;
            }

            /* Cross-book arb comparison */
            .pq-arb-compare {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 1rem 1.1rem;
                margin: 0.75rem 0 1rem;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-arb-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 0.7rem;
            }
            @media (max-width: 640px) {
                .pq-arb-grid { grid-template-columns: 1fr; }
            }
            .pq-book-card {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.9rem;
            }
            .pq-book-header {
                font-size: 0.66rem;
                font-weight: 800;
                color: var(--pq-muted);
                text-transform: uppercase;
                letter-spacing: 0.10em;
                margin-bottom: 0.35rem;
            }
            .pq-book-title {
                font-size: 0.84rem;
                font-weight: 800;
                color: var(--pq-text);
                line-height: 1.35;
                margin-bottom: 0.65rem;
                min-height: 2.2rem;
                letter-spacing: -0.01em;
            }
            .pq-odd-row {
                display: flex; justify-content: space-between; align-items: center;
                padding: 0.5rem 0.6rem;
                border-radius: 9px;
                margin-bottom: 0.4rem;
                font-size: 0.82rem; font-weight: 800;
                font-variant-numeric: tabular-nums;
            }
            .pq-odd-row.yes {
                background: rgba(52,211,153,0.10);
                border: 1px solid rgba(52,211,153,0.30);
                color: var(--pq-success);
            }
            .pq-odd-row.no {
                background: rgba(248,113,113,0.07);
                border: 1px solid rgba(248,113,113,0.25);
                color: var(--pq-danger);
            }
            .pq-odd-row .pq-odd-val {
                font-weight: 900; color: var(--pq-text); font-size: 0.80rem;
            }
            .pq-strategy-card {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 1rem 1.1rem;
                margin: 0.65rem 0;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-strategy-card.pq-strategy-live {
                border-color: rgba(52,211,153,0.55);
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 18px 42px -22px rgba(16,185,129,0.50);
            }
            .pq-strategy-head {
                display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 0.65rem; flex-wrap: wrap; gap: 0.35rem;
            }
            .pq-strategy-title {
                font-size: 0.95rem; font-weight: 900; color: var(--pq-text);
                margin: 0; letter-spacing: -0.01em;
            }
            .pq-strategy-badge {
                font-size: 0.66rem; font-weight: 900;
                padding: 0.28rem 0.6rem;
                border-radius: 999px;
                text-transform: uppercase; letter-spacing: 0.06em;
            }
            .pq-strategy-badge.live {
                background: rgba(52,211,153,0.18);
                color: var(--pq-success);
                border: 1px solid rgba(52,211,153,0.50);
            }
            .pq-strategy-badge.dead {
                background: rgba(148,163,184,0.10);
                color: var(--pq-muted);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-strategy-metrics {
                display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 0.5rem; margin-top: 0.7rem;
            }
            @media (max-width: 480px) {
                .pq-strategy-metrics { grid-template-columns: 1fr; }
            }
            .pq-metric-box {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.6rem 0.7rem;
                text-align: center;
            }
            .pq-metric-box .lbl {
                display: block; font-size: 0.60rem; font-weight: 800;
                color: var(--pq-muted);
                text-transform: uppercase; letter-spacing: 0.08em;
            }
            .pq-metric-box .val {
                display: block; font-size: 1rem; font-weight: 900;
                color: var(--pq-text); margin-top: 0.2rem;
                font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
            }
            .pq-metric-box .val.green { color: var(--pq-success); }
            .pq-metric-box .val.red   { color: var(--pq-danger); }
            .pq-arb-detail {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.85rem;
                margin-top: 0.7rem;
            }
            .pq-arb-detail-title {
                margin: 0 0 0.5rem;
                color: var(--pq-text);
                font-size: 0.74rem; font-weight: 900;
                text-transform: uppercase; letter-spacing: 0.10em;
            }
            .pq-arb-ticket-row {
                display: grid; grid-template-columns: 1.25fr 0.75fr 0.8fr 0.9fr;
                gap: 0.5rem; align-items: center;
                padding: 0.55rem 0;
                border-top: 1px solid var(--pq-border);
                font-size: 0.8rem; color: var(--pq-text-2);
                font-variant-numeric: tabular-nums;
            }
            .pq-arb-ticket-row.header {
                border-top: 0; padding-top: 0;
                color: var(--pq-muted);
                font-size: 0.62rem; font-weight: 900;
                text-transform: uppercase; letter-spacing: 0.08em;
            }
            .pq-arb-ticket-row strong { color: var(--pq-text); font-weight: 800; }
            .pq-arb-ticket-row .cash {
                color: var(--pq-accent); font-weight: 900; text-align: right;
            }
            .pq-arb-explain {
                margin: 0.65rem 0 0;
                color: var(--pq-text-2);
                font-size: 0.84rem; line-height: 1.5;
            }
            .pq-arb-explain strong { color: var(--pq-text); }
            .pq-arb-warning {
                background: rgba(248,113,113,0.10);
                border: 1px solid rgba(248,113,113,0.45);
                border-radius: var(--pq-radius);
                color: #fecaca;
                font-size: 0.82rem; line-height: 1.5;
                margin-top: 0.7rem; padding: 0.75rem 0.85rem;
            }
            .pq-arb-spotlight {
                position: relative;
                background:
                    radial-gradient(ellipse 80% 50% at 10% 0%, rgba(129,140,248,0.28), transparent 70%),
                    linear-gradient(180deg, rgba(129,140,248,0.10), var(--pq-surface-1));
                border: 1px solid rgba(129,140,248,0.55);
                border-radius: var(--pq-radius-xl);
                box-shadow: 0 0 0 1px rgba(129,140,248,0.10) inset,
                            0 22px 50px -24px rgba(99,102,241,0.55);
                margin: 0.75rem 0 1rem;
                padding: 1.05rem;
            }
            .pq-arb-spotlight.live {
                background:
                    radial-gradient(ellipse 80% 50% at 10% 0%, rgba(52,211,153,0.30), transparent 70%),
                    linear-gradient(180deg, rgba(52,211,153,0.10), var(--pq-surface-1));
                border-color: rgba(52,211,153,0.60);
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 22px 56px -24px rgba(16,185,129,0.60);
            }
            .pq-arb-spotlight.dead {
                background:
                    radial-gradient(ellipse 80% 50% at 10% 0%, rgba(248,113,113,0.24), transparent 70%),
                    linear-gradient(180deg, rgba(248,113,113,0.08), var(--pq-surface-1));
                border-color: rgba(248,113,113,0.55);
            }
            .pq-arb-spotlight-kicker {
                color: var(--pq-muted);
                font-size: 0.66rem; font-weight: 900;
                letter-spacing: 0.12em;
                margin: 0 0 0.25rem;
                text-transform: uppercase;
            }
            .pq-arb-spotlight-title {
                color: var(--pq-text);
                font-size: 1.15rem; font-weight: 900;
                letter-spacing: -0.02em; line-height: 1.2;
                margin: 0 0 0.7rem;
            }
            .pq-arb-action-list { display: grid; gap: 0.55rem; margin: 0.75rem 0; }
            .pq-arb-action {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.8rem;
                transition: border-color .15s ease, transform .15s ease;
            }
            .pq-arb-action:hover { border-color: var(--pq-border-strong); transform: translateY(-1px); }
            .pq-arb-action .step {
                color: var(--pq-muted); display: block;
                font-size: 0.64rem; font-weight: 900;
                letter-spacing: 0.10em; text-transform: uppercase;
            }
            .pq-arb-action .take {
                color: var(--pq-text); display: block;
                font-size: 0.96rem; font-weight: 900;
                margin-top: 0.18rem; letter-spacing: -0.01em;
            }
            .pq-arb-action .meta {
                color: var(--pq-accent); display: block;
                font-size: 0.78rem; font-weight: 800;
                margin-top: 0.2rem;
                font-variant-numeric: tabular-nums;
            }
            .pq-arb-spotlight-note {
                color: var(--pq-text-2);
                font-size: 0.84rem; line-height: 1.5;
                margin: 0.65rem 0 0;
            }
            .pq-arb-spotlight-note strong { color: var(--pq-text); }
            @media (max-width: 480px) {
                .pq-arb-ticket-row {
                    grid-template-columns: 1fr 0.62fr;
                    gap: 0.28rem 0.45rem;
                }
                .pq-arb-ticket-row.header { display: none; }
                .pq-arb-ticket-row .cash { text-align: left; }
            }

            /* Kalshi auto-suggest */
            .pq-suggest-card {
                background: var(--pq-surface-2);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.75rem 0.9rem;
                margin-bottom: 0.4rem;
            }
            .pq-suggest-score {
                display: inline-block;
                font-size: 0.62rem;
                font-weight: 900;
                color: var(--pq-accent);
                background: rgba(129,140,248,0.12);
                border: 1px solid rgba(129,140,248,0.40);
                border-radius: 999px;
                padding: 0.17rem 0.5rem;
                margin-bottom: 0.35rem;
                letter-spacing: 0.06em;
                text-transform: uppercase;
            }
            .pq-suggest-title {
                display: block; font-size: 0.84rem; font-weight: 700;
                color: var(--pq-text); line-height: 1.35;
            }
            .pq-suggest-meta {
                display: block; font-size: 0.72rem;
                color: var(--pq-accent); font-weight: 700; margin-top: 0.2rem;
            }
            .pq-build-tag {
                color: var(--pq-accent); font-weight: 800;
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.7rem;
            }

            /* Pikkit-style performance calendar */
            .pq-perf-calendar {
                background: linear-gradient(180deg, var(--pq-surface-1), var(--pq-surface-2));
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-lg);
                padding: 0.95rem 1.05rem 1.05rem;
                margin: 0.7rem 0 1.1rem;
                box-shadow: var(--pq-shadow-soft);
            }
            .pq-perf-cal-header {
                display: flex; justify-content: space-between; align-items: baseline;
                margin-bottom: 0.75rem; flex-wrap: wrap; gap: 0.35rem;
            }
            .pq-perf-cal-title {
                font-size: 1rem; font-weight: 900;
                color: var(--pq-text); letter-spacing: -0.02em;
            }
            .pq-perf-cal-sub {
                font-size: 0.72rem; font-weight: 700; color: var(--pq-muted);
                text-transform: uppercase; letter-spacing: 0.06em;
            }
            .pq-perf-cal-month-pnl {
                font-size: 0.85rem; font-weight: 900;
                font-variant-numeric: tabular-nums;
                padding: 0.3rem 0.6rem;
                border-radius: 999px;
                border: 1px solid var(--pq-border);
            }
            .pq-perf-cal-month-pnl.pos {
                color: var(--pq-success);
                background: rgba(52,211,153,0.10);
                border-color: rgba(52,211,153,0.35);
            }
            .pq-perf-cal-month-pnl.neg {
                color: var(--pq-danger);
                background: rgba(248,113,113,0.10);
                border-color: rgba(248,113,113,0.35);
            }
            .pq-perf-cal-month-pnl.flat { color: var(--pq-muted); }
            .pq-perf-cal-grid {
                display: grid;
                grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 6px;
            }
            .pq-perf-cal-head {
                text-align: center; font-size: 0.62rem; font-weight: 900;
                color: var(--pq-faint); text-transform: uppercase;
                letter-spacing: 0.10em; padding: 0.2rem 0 0.4rem;
            }
            .pq-perf-cal-cell {
                min-height: 60px;
                border-radius: 9px;
                border: 1px solid var(--pq-border);
                background: var(--pq-surface-2);
                padding: 0.4rem 0.35rem 0.35rem;
                display: flex; flex-direction: column;
                justify-content: space-between; align-items: stretch;
                transition: transform .12s ease;
            }
            .pq-perf-cal-cell:hover { transform: translateY(-1px); }
            .pq-perf-cal-cell.pq-perf-empty {
                background: transparent; border-color: transparent;
                min-height: 0; padding: 0;
            }
            .pq-perf-cal-cell.pq-perf-today {
                box-shadow: 0 0 0 2px var(--pq-accent);
            }
            .pq-perf-cal-cell.pq-perf-win {
                background: rgba(52,211,153,0.14);
                border-color: rgba(52,211,153,0.45);
            }
            .pq-perf-cal-cell.pq-perf-loss {
                background: rgba(248,113,113,0.12);
                border-color: rgba(248,113,113,0.40);
            }
            .pq-perf-cal-cell.pq-perf-flat {
                background: var(--pq-surface-2);
                border-color: var(--pq-border-strong);
            }
            .pq-perf-cal-day {
                font-size: 0.62rem; font-weight: 800;
                color: var(--pq-muted); line-height: 1;
            }
            .pq-perf-cal-pnl {
                font-size: 0.74rem; font-weight: 900;
                text-align: center; line-height: 1.1;
                margin-top: 0.15rem;
                font-variant-numeric: tabular-nums;
            }
            .pq-perf-cal-pnl.pos { color: var(--pq-success); }
            .pq-perf-cal-pnl.neg { color: var(--pq-danger); }
            .pq-perf-cal-pnl.flat { color: var(--pq-text-2); }
            .pq-perf-cal-count {
                font-size: 0.58rem; font-weight: 700;
                color: var(--pq-faint);
                text-align: center; margin-top: 0.1rem;
                text-transform: uppercase; letter-spacing: 0.04em;
            }

            /* Ledger calendar flexbox (legacy) */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid { display: flex; flex-wrap: wrap; gap: 4px; }
            .pq-cal-head {
                flex: 1 0 calc(14.28% - 4px); min-width: 0;
                text-align: center; font-size: 0.65rem; font-weight: 800;
                color: var(--pq-muted); padding: 0.25rem 0;
                text-transform: uppercase; letter-spacing: 0.08em;
            }
            .pq-cal-cell {
                flex: 1 0 calc(14.28% - 4px); min-width: 0;
                aspect-ratio: 1; border-radius: 9px;
                border: 1px solid var(--pq-border);
                position: relative;
                display: flex; align-items: center; justify-content: center;
            }
            .pq-cal-day {
                position: absolute; top: 4px; left: 6px;
                font-size: 0.62rem; color: var(--pq-muted); font-weight: 700;
            }
            .pq-cal-neutral { background: var(--pq-surface-2); }
            .pq-cal-win { background: rgba(52,211,153,0.18); border-color: rgba(52,211,153,0.45); }
            .pq-cal-loss { background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.40); }
            .pq-cal-pnl { font-size: 0.72rem; font-weight: 900; font-variant-numeric: tabular-nums; }
            .pq-cal-pnl.pos { color: var(--pq-success); }
            .pq-cal-pnl.neg { color: var(--pq-danger); }
            .pq-cal-dash { color: var(--pq-faint); font-size: 0.85rem; }


            /* Elite value plays (SOP) */
            .pq-value-card-elite {
                border: 1px solid rgba(52,211,153,0.65);
                box-shadow: 0 0 0 1px rgba(52,211,153,0.10) inset,
                            0 22px 50px -22px rgba(16,185,129,0.55);
            }
            .pq-rank-badge {
                display: inline-block;
                background: rgba(52,211,153,0.14);
                color: var(--pq-success);
                border: 1px solid rgba(52,211,153,0.45);
                font-weight: 900; font-size: 0.72rem;
                padding: 0.30rem 0.65rem; border-radius: 999px;
                margin-bottom: 0.5rem; letter-spacing: 0.06em;
                text-transform: uppercase;
            }
            .pq-rank-badge-elite {
                background: linear-gradient(135deg, rgba(52,211,153,0.30), rgba(34,211,238,0.20));
                border-color: rgba(52,211,153,0.70);
                font-size: 0.76rem;
                color: #d1fae5;
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
                scrollbar-width: none;
            }
            .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar { display: none; }
            .stTabs [data-baseweb="tab"] {
                white-space: nowrap !important;
                flex-shrink: 0 !important;
            }

            /* Dataframe / table polish */
            [data-testid="stDataFrame"] [role="columnheader"] {
                background: var(--pq-surface-2) !important;
                color: var(--pq-muted) !important;
                font-weight: 800 !important;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                font-size: 0.7rem !important;
            }
            [data-testid="stDataFrame"] [role="row"]:hover {
                background: rgba(129,140,248,0.04) !important;
            }

            /* Expander polish */
            .streamlit-expanderHeader, [data-testid="stExpander"] summary {
                background: var(--pq-surface-1) !important;
                border-radius: var(--pq-radius) !important;
                font-weight: 700 !important;
                color: var(--pq-text) !important;
            }
            [data-testid="stExpander"] {
                border: 1px solid var(--pq-border) !important;
                border-radius: var(--pq-radius) !important;
                background: var(--pq-surface-1) !important;
            }

            /* Headings */
            h1, h2, h3, h4 {
                letter-spacing: -0.02em;
                color: var(--pq-text);
            }
            h3 { font-weight: 800; }
            h4 { font-weight: 700; color: var(--pq-text-2); }

            .block-container { max-width: 1180px; padding-bottom: 2rem; }

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
    abs_delta = abs(delta)

    m1, m2, m3 = st.columns(3)
    m1.metric("Divergence", f"{delta:+.1f}%", help="Sentiment minus true probability")
    m2.metric("Trigger Threshold", f"±{DIVERGENCE_TRIGGER:.0f}%",
              help="Above this, the narrative is diverging from the math")
    severity = "Hot" if abs_delta >= DIVERGENCE_TRIGGER else "Mild" if abs_delta >= 8 else "Aligned"
    m3.metric("Signal", severity)

    if delta >= DIVERGENCE_TRIGGER:
        st.markdown(
            f'<div class="pq-bubble-badge">🔥 Narrative bubble · '
            f'crowd is {delta:+.0f}% above true probability — consider fading the public</div>',
            unsafe_allow_html=True,
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.markdown(
            f'<div class="pq-card pq-card-compound">'
            f'<p class="pq-card-title">💎 Crowd too bearish</p>'
            f'<p style="margin:0;color:var(--pq-text-2);font-size:0.86rem;line-height:1.45;">'
            f'Sentiment lags the math by {abs_delta:.0f}% — YES may be cheap relative to the model.</p>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="pq-card">'
            f'<p class="pq-card-title" style="color:var(--pq-muted);">⏸ Aligned</p>'
            f'<p style="margin:0;color:var(--pq-faint);font-size:0.84rem;line-height:1.45;">'
            f'Crowd and model are within ±{DIVERGENCE_TRIGGER:.0f}% — no narrative edge right now.</p>'
            f'</div>',
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
        st.session_state.poly_selected_picker_open = False
        st.session_state.arb_poly_anchor = None
    else:
        st.session_state.kalshi_selected = row["id"]
        st.session_state.kalshi_selected_picker_open = False


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


def _build_arb_strategy(
    key: str,
    label: str,
    poly_side: str,
    poly_price: float,
    kalshi_side: str,
    kalshi_price: float,
    stake: float,
) -> dict[str, Any]:
    """Normalize all displayed math for one cross-book strategy."""
    total_cost = poly_price + kalshi_price
    _, roi = _arb_opportunity(total_cost)
    is_arb = total_cost < 1.0
    contracts = stake
    poly_cash = contracts * poly_price
    kalshi_cash = contracts * kalshi_price
    total_outlay = poly_cash + kalshi_cash
    guaranteed_payout = contracts
    profit = guaranteed_payout - total_outlay
    break_even_gap = 1.0 - total_cost
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
        "profit": profit,
        "break_even_gap": break_even_gap,
    }


def _best_arb_strategy(strategies: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the lowest combined-cost strategy; live arbs naturally rank first."""
    return min(strategies, key=lambda item: float(item["total_cost"]))


def _render_selected_arb_spotlight(strategy: dict[str, Any], odds_fmt: str) -> None:
    """Primary main-screen instruction card for the currently selected strategy."""
    is_arb = bool(strategy["is_arb"])
    cls = "live" if is_arb else "dead"
    kicker = "TAKE THIS ARB" if is_arb else "NO SAFE ARB"
    title = (
        "Place these two legs for a locked payout"
        if is_arb
        else "Do not place this pair yet - closest strategy shown"
    )

    poly_side = str(strategy["poly_side"])
    kalshi_side = str(strategy["kalshi_side"])
    poly_price = float(strategy["poly_price"])
    kalshi_price = float(strategy["kalshi_price"])
    contracts = float(strategy["contracts"])
    guaranteed_payout = float(strategy["guaranteed_payout"])
    total_outlay = float(strategy["total_outlay"])
    profit = float(strategy["profit"])
    total_c = float(strategy["total_cost"]) * 100.0
    profit_text = _signed_money(profit)
    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    edge_text = (
        f"Locked edge: {float(strategy['break_even_gap']) * 100:.1f}c per $1."
        if is_arb
        else f"Needs {abs(float(strategy['break_even_gap'])) * 100:.1f}c improvement before it is an arb."
    )

    st.markdown(
        f"""
        <div class="pq-arb-spotlight {cls}">
            <p class="pq-arb-spotlight-kicker">{kicker}</p>
            <p class="pq-arb-spotlight-title">{html.escape(title)}</p>
            <div class="pq-arb-action-list">
                <div class="pq-arb-action">
                    <span class="step">Step 1 - Polymarket</span>
                    <span class="take">Buy {html.escape(poly_side)} · {poly_price * 100:.1f}% ({html.escape(poly_odds)})</span>
                    <span class="meta">{contracts:,.0f} contracts · spend ${float(strategy['poly_cash']):,.2f}</span>
                </div>
                <div class="pq-arb-action">
                    <span class="step">Step 2 - Kalshi</span>
                    <span class="take">Buy {html.escape(kalshi_side)} · {kalshi_price * 100:.1f}% ({html.escape(kalshi_odds)})</span>
                    <span class="meta">{contracts:,.0f} contracts · spend ${float(strategy['kalshi_cash']):,.2f}</span>
                </div>
            </div>
            <p class="pq-arb-spotlight-note">
                Combined cost is <strong>{total_c:.1f}c</strong>. Total cash needed is
                <strong>${total_outlay:,.2f}</strong> for a <strong>${guaranteed_payout:,.2f}</strong>
                payout on either outcome. Result before fees/slippage:
                <strong>{profit_text}</strong>. {html.escape(edge_text)}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_arb_strategy_card(
    strategy: dict[str, Any],
    odds_fmt: str,
    *,
    selected: bool = False,
) -> None:
    """One arb recipe with a full ticket and settlement math."""
    label = str(strategy["label"])
    poly_side = str(strategy["poly_side"])
    poly_price = float(strategy["poly_price"])
    kalshi_side = str(strategy["kalshi_side"])
    kalshi_price = float(strategy["kalshi_price"])
    total_cost = float(strategy["total_cost"])
    roi = float(strategy["roi"])
    is_arb = bool(strategy["is_arb"])
    contracts = float(strategy["contracts"])
    poly_cash = float(strategy["poly_cash"])
    kalshi_cash = float(strategy["kalshi_cash"])
    total_outlay = float(strategy["total_outlay"])
    guaranteed_payout = float(strategy["guaranteed_payout"])
    profit = float(strategy["profit"])
    break_even_gap = float(strategy["break_even_gap"])
    profit_text = _signed_money(profit)

    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    poly_c = poly_price * 100.0
    kalshi_c = kalshi_price * 100.0
    total_c = total_cost * 100.0

    card_cls = "pq-strategy-card pq-strategy-live" if is_arb else "pq-strategy-card"
    badge_cls = "pq-strategy-badge live" if is_arb else "pq-strategy-badge dead"
    badge_txt = "Selected" if selected else ("Arb locked" if is_arb else "No lock")
    profit_class = "green" if is_arb else "red"
    profit_label = "Guaranteed profit" if is_arb else "Worst-case loss"
    outcome_a = f"If the event resolves {poly_side}, Polymarket pays ${guaranteed_payout:,.2f}."
    outcome_b = f"If the event resolves {kalshi_side}, Kalshi pays ${guaranteed_payout:,.2f}."
    pricing_note = (
        f"You are paying {total_c:.1f}c for $1.00 of coverage, leaving "
        f"{break_even_gap * 100:.1f}c of locked edge per contract."
        if is_arb
        else (
            f"This costs {total_c:.1f}c for $1.00 of coverage. It needs to be below "
            f"100.0c, so wait for at least {(total_cost - 1.0) * 100:.1f}c of improvement."
        )
    )

    lock_html = ""
    if is_arb:
        lock_html = (
            f'<div class="pq-lock-banner">Guaranteed +${profit:.2f} profit on '
            f"${total_outlay:,.2f} total outlay</div>"
        )
    else:
        lock_html = (
            '<div class="pq-arb-warning"><strong>Not a risk-free bet yet.</strong> '
            f"At these prices, the paired ticket loses ${abs(profit):,.2f} before fees "
            "or slippage. Do not place it as an arb unless the combined cost drops under 100c.</div>"
        )

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
                        {poly_c:.1f}¢ · {html.escape(poly_odds)} · {contracts:,.0f} contracts
                    </div>
                </div>
                <div class="pq-split-side">
                    <div class="venue">Kalshi</div>
                    <div class="leg">Buy {html.escape(kalshi_side)}</div>
                    <div style="font-size:0.78rem;color:#8b949e;margin-top:0.35rem;">
                        {kalshi_c:.1f}¢ · {html.escape(kalshi_odds)} · {contracts:,.0f} contracts
                    </div>
                </div>
            </div>
            <div class="pq-strategy-metrics">
                <div class="pq-metric-box">
                    <span class="lbl">Combined cost</span>
                    <span class="val">{total_c:.1f}¢ / $1</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">{profit_label}</span>
                    <span class="val {profit_class}">{profit_text}</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">ROI on outlay</span>
                    <span class="val {profit_class}">{roi:+.2f}%</span>
                </div>
            </div>
            <div class="pq-arb-detail">
                <p class="pq-arb-detail-title">Exact bet ticket for ${guaranteed_payout:,.0f} payout</p>
                <div class="pq-arb-ticket-row header">
                    <span>Book / side</span><span>Price</span><span>Contracts</span><span class="cash">Cash needed</span>
                </div>
                <div class="pq-arb-ticket-row">
                    <span><strong>Polymarket {html.escape(poly_side)}</strong></span>
                    <span>{poly_c:.1f}c</span>
                    <span>{contracts:,.2f}</span>
                    <span class="cash">${poly_cash:,.2f}</span>
                </div>
                <div class="pq-arb-ticket-row">
                    <span><strong>Kalshi {html.escape(kalshi_side)}</strong></span>
                    <span>{kalshi_c:.1f}c</span>
                    <span>{contracts:,.2f}</span>
                    <span class="cash">${kalshi_cash:,.2f}</span>
                </div>
                <p class="pq-arb-explain">
                    Total cash outlay is <strong>${total_outlay:,.2f}</strong>.
                    {html.escape(outcome_a)} {html.escape(outcome_b)}
                    Guaranteed payout is <strong>${guaranteed_payout:,.2f}</strong>, so the locked result is
                    <strong>{profit_text}</strong> before fees and execution slippage.
                </p>
                <p class="pq-arb-explain">{html.escape(pricing_note)}</p>
            </div>
        </div>
        {lock_html}
        """,
        unsafe_allow_html=True,
    )
    if st.button(
        "Showing on main screen" if selected else "Show this strategy on main screen",
        key=f"arb_strategy_select_{strategy['key']}",
        use_container_width=True,
        type="primary" if selected else "secondary",
    ):
        st.session_state.arb_selected_strategy = strategy["key"]
        st.rerun()


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
    st.caption(
        "Pick one market on each exchange. Each strategy now shows exact contract sizing, "
        "cash needed per book, payout, profit, and the no-lock warning when prices are too high."
    )

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        arb_stake = st.number_input(
            "Target payout / contracts",
            min_value=1.0,
            value=DEFAULT_ARB_STAKE,
            step=10.0,
            key="arb_stake",
            help=(
                "Arb sizing uses equal contracts on both books. "
                "A value of 100 means buy 100 Polymarket contracts and 100 Kalshi contracts, "
                "so either outcome pays $100 before fees."
            ),
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
        row["id"]: _book_price_hint(
            "Polymarket",
            float(row["Yes Price"]),
            float(row["No Price"]),
            odds_fmt,
        )
        for _, row in poly_priced.iterrows()
    }
    kalshi_options = {row["ticker"]: row["Title"] for _, row in kalshi_priced.iterrows()}
    kalshi_prices = {
        row["ticker"]: _book_price_hint(
            "Kalshi",
            float(row["Kalshi YES Cost"]),
            float(row["Kalshi NO Cost"]),
            odds_fmt,
        )
        for _, row in kalshi_priced.iterrows()
    }

    poly_id = render_searchable_picker(
        "Polymarket Event",
        poly_options,
        "poly_selected",
        show_prices=poly_prices,
        collapse_after_select=True,
    )
    if not poly_id:
        return

    poly_title = poly_options[poly_id]
    suggestions = _sync_kalshi_auto_suggest(poly_id, poly_title, kalshi_priced)
    _render_kalshi_suggestions(suggestions, kalshi_prices)

    kalshi_ticker = render_searchable_picker(
        "Kalshi Event",
        kalshi_options,
        "kalshi_selected",
        show_prices=kalshi_prices,
        collapse_after_select=True,
    )
    if not kalshi_ticker:
        return

    poly_row = poly_priced.loc[poly_priced["id"] == poly_id].iloc[0]
    kalshi_row = kalshi_priced.loc[kalshi_priced["ticker"] == kalshi_ticker].iloc[0]

    poly_yes = float(poly_row["Yes Price"])
    poly_no = float(poly_row["No Price"])
    kalshi_yes = float(kalshi_row["Kalshi YES Cost"])
    kalshi_no = float(kalshi_row["Kalshi NO Cost"])

    strategies = [
        _build_arb_strategy(
            "strategy_a",
            "Strategy A - Poly YES + Kalshi NO",
            "YES",
            poly_yes,
            "NO",
            kalshi_no,
            arb_stake,
        ),
        _build_arb_strategy(
            "strategy_b",
            "Strategy B - Poly NO + Kalshi YES",
            "NO",
            poly_no,
            "YES",
            kalshi_yes,
            arb_stake,
        ),
    ]
    pair_key = f"{poly_id}::{kalshi_ticker}"
    valid_strategy_keys = {str(item["key"]) for item in strategies}
    if (
        st.session_state.get("arb_selected_pair") != pair_key
        or st.session_state.get("arb_selected_strategy") not in valid_strategy_keys
    ):
        best = _best_arb_strategy(strategies)
        st.session_state.arb_selected_pair = pair_key
        st.session_state.arb_selected_strategy = best["key"]

    selected_key = st.session_state.get("arb_selected_strategy")
    selected_strategy = next(
        (item for item in strategies if item["key"] == selected_key),
        _best_arb_strategy(strategies),
    )
    _render_selected_arb_spotlight(selected_strategy, odds_fmt)
    _render_cross_book_odds(poly_row, kalshi_row, odds_fmt)

    st.markdown('<p class="pq-section-label">Arb strategies</p>', unsafe_allow_html=True)

    for strategy in strategies:
        _render_arb_strategy_card(
            strategy,
            odds_fmt,
            selected=strategy["key"] == selected_strategy["key"],
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

_inject_global_css()
_init_session()


# --------------------------------------------------------------------------- #
# App shell — modern header, status, market pulse, onboarding, tab headers
# --------------------------------------------------------------------------- #


def _safe_pulse_stats() -> dict[str, Any]:
    """Compute lightweight market-pulse stats with safe fallbacks."""
    stats: dict[str, Any] = {
        "markets_tracked": None,
        "anomalies": None,
        "total_volume": None,
        "best_edge": None,
        "avg_edge": None,
        "kalshi_markets": None,
        "today_pnl": None,
        "data_status": "Live",
    }

    try:
        poly_df = fetch_polymarket_markets()
    except Exception:
        poly_df = pd.DataFrame()
        stats["data_status"] = "Cached"

    try:
        kalshi_df = fetch_kalshi_markets()
    except Exception:
        kalshi_df = pd.DataFrame()

    poly_n = int(len(poly_df)) if not poly_df.empty else 0
    kalshi_n = int(len(kalshi_df)) if not kalshi_df.empty else 0
    stats["markets_tracked"] = poly_n + kalshi_n
    stats["kalshi_markets"] = kalshi_n

    if not poly_df.empty and "Volume" in poly_df.columns:
        try:
            stats["total_volume"] = float(pd.to_numeric(poly_df["Volume"], errors="coerce").fillna(0).sum())
        except Exception:
            stats["total_volume"] = None

    try:
        elite_df = _filter_value_plays(poly_df) if not poly_df.empty else pd.DataFrame()
        stats["anomalies"] = int(len(elite_df))
        if not elite_df.empty and "Net EV Edge %" in elite_df.columns:
            edges = pd.to_numeric(elite_df["Net EV Edge %"], errors="coerce").dropna()
            if len(edges):
                stats["best_edge"] = float(edges.max())
                stats["avg_edge"] = float(edges.mean())
    except Exception:
        pass

    try:
        ledger = fetch_unified_ledger()
        if not ledger.empty:
            today = datetime.now(timezone.utc).date()
            daily = _ledger_daily_pnl(ledger)
            stats["today_pnl"] = float(daily.get(today, 0.0))
    except Exception:
        pass

    return stats


def _fmt_volume(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def _render_status_strip() -> None:
    """Slim status pill: live data + build fingerprint."""
    st.markdown(
        f"""
        <div class="pq-status-strip">
            <span class="pq-status-dot"></span>
            <span class="pq-status-live">LIVE</span>
            <span>· Polymarket &amp; Kalshi feeds active</span>
            <span style="margin-left:auto;">
                Build <code>{html.escape(APP_BUILD)}</code>
                <span style="opacity:0.7;">· {html.escape(GIT_SHA)}</span>
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_app_header() -> None:
    """Modern brand header with animated live indicator."""
    st.markdown(
        f"""
        <div class="pq-topbar">
            <span class="pq-topbar-brand">
                <span class="pq-brand-mark">PQ</span>
                <span class="pq-brand-name">POLY-QUANT</span>
                <span class="pq-brand-tag">Terminal</span>
            </span>
            <span class="pq-topbar-meta">
                <span class="dot"></span>
                Polymarket &amp; Kalshi · cross-book intelligence
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_market_pulse(stats: dict[str, Any]) -> None:
    """Data-driven KPI strip at the top of the app."""
    markets = stats.get("markets_tracked")
    anomalies = stats.get("anomalies")
    total_vol = stats.get("total_volume")
    best_edge = stats.get("best_edge")
    avg_edge = stats.get("avg_edge")
    today_pnl = stats.get("today_pnl")
    kalshi_n = stats.get("kalshi_markets")

    def tile(label: str, value: str, sub: str, accent: str = "",
             value_cls: str = "") -> str:
        return f"""
            <div class="pq-pulse-tile {accent}">
                <div class="pq-pulse-label">{html.escape(label)}</div>
                <div class="pq-pulse-value {value_cls}">{value}</div>
                <div class="pq-pulse-sub">{sub}</div>
            </div>
        """

    markets_val = f"{markets:,}" if markets else "—"
    markets_sub = f"Kalshi adds {kalshi_n:,}" if kalshi_n else "Polymarket Gamma feed"

    anomalies_val = f"{anomalies}" if anomalies is not None else "—"
    anomalies_sub = "Elite NO-side edges" if anomalies else "No anomalies — hold fire"

    vol_val = _fmt_volume(total_vol)
    vol_sub = "24h Polymarket notional"

    if best_edge is not None:
        edge_val = f"+{best_edge:.1f}%"
        edge_sub = f"Avg edge {avg_edge:+.1f}%" if avg_edge is not None else "Net EV after fees"
    else:
        edge_val = "—"
        edge_sub = "Awaiting fresh anomalies"

    if today_pnl is None:
        pnl_val = "—"
        pnl_sub = "Connect a book to track"
        pnl_cls = ""
    elif today_pnl > 0:
        pnl_val = f"+${today_pnl:,.0f}"
        pnl_sub = "Today, all books"
        pnl_cls = "pos"
    elif today_pnl < 0:
        pnl_val = f"-${abs(today_pnl):,.0f}"
        pnl_sub = "Today, all books"
        pnl_cls = "neg"
    else:
        pnl_val = "$0"
        pnl_sub = "Flat session"
        pnl_cls = ""

    tiles_html = (
        tile("Markets Tracked", markets_val, markets_sub, "accent-cyan")
        + tile("Live Anomalies", anomalies_val, anomalies_sub, "accent-emerald")
        + tile("Top Edge", edge_val, edge_sub)
        + tile("24h Volume", vol_val, vol_sub, "accent-amber")
        + tile("Your P&L Today", pnl_val, pnl_sub,
               "accent-rose" if pnl_cls == "neg" else "accent-emerald" if pnl_cls == "pos" else "",
               pnl_cls)
    )

    st.markdown(
        f'<div class="pq-pulse-grid">{tiles_html}</div>',
        unsafe_allow_html=True,
    )


def _render_onboarding_guide() -> None:
    """Collapsible 'How POLY-QUANT works' guide for new users."""
    with st.expander("🧭 How POLY-QUANT works — quick start", expanded=False):
        st.markdown(
            """
            <div class="pq-onboard">
                <p class="pq-onboard-title">Four steps from idea to execution</p>
                <div class="pq-onboard-steps">
                    <div class="pq-onboard-step">
                        <span class="num">1</span>
                        <span class="body">
                            <strong>Scan Value Plays</strong>
                            <span>Elite NO-side edges where the model beats the line by 5%+ net of fees.</span>
                        </span>
                    </div>
                    <div class="pq-onboard-step">
                        <span class="num">2</span>
                        <span class="body">
                            <strong>Explore the catalog</strong>
                            <span>Filter Polymarket + Kalshi by sport, category, or your own search.</span>
                        </span>
                    </div>
                    <div class="pq-onboard-step">
                        <span class="num">3</span>
                        <span class="body">
                            <strong>Audit your bet</strong>
                            <span>Run any price through the EV engine and get a clear PLAY / PASS verdict.</span>
                        </span>
                    </div>
                    <div class="pq-onboard-step">
                        <span class="num">4</span>
                        <span class="body">
                            <strong>Lock cross-book arbs</strong>
                            <span>Pair Polymarket vs Kalshi for guaranteed-return tickets when costs sum below $1.00.</span>
                        </span>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_tab_head(emoji: str, title: str, subtitle: str, tip: str | None = None) -> None:
    """Consistent per-tab header with title, subtitle, and an optional workflow hint."""
    st.markdown(
        f"""
        <div class="pq-tab-head">
            <div>
                <h2>{emoji} {html.escape(title)}</h2>
                <p class="pq-tab-sub">{html.escape(subtitle)}</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if tip:
        st.markdown(
            f'<div class="pq-tab-hint">{tip}</div>',
            unsafe_allow_html=True,
        )


_render_status_strip()
_render_app_header()

try:
    _render_market_pulse(_safe_pulse_stats())
except Exception:
    pass

_render_onboarding_guide()

tool_l, tool_r = st.columns([3, 1])
with tool_l:
    render_global_search_bar()
with tool_r:
    render_odds_format_toggle()


_TAB_HEADERS: dict[str, dict[str, str]] = {
    "plays": {
        "emoji": "🔥",
        "title": "Top Value Plays",
        "subtitle": f"Elite tier · win prob > {VALUE_PLAYS_WIN_MIN:.0f}% · net EV ≥ {VALUE_PLAYS_EV_EDGE_MIN:.0f}%",
        "tip": "<strong>Read it like a hot list.</strong> Each row is a NO-side edge our model says the market is mispricing. "
               "Sort by <em>Quant Edge</em> and start with the biggest — but check liquidity before sizing.",
    },
    "explore": {
        "emoji": "🔍",
        "title": "Explore the Market Catalog",
        "subtitle": "Browse every active Polymarket + Kalshi contract — filter, search, then send into Arbs or Audit.",
        "tip": "<strong>One-tap workflow.</strong> Hit <em>Select market</em> on any row to load it into the Arbs and Audit tabs instantly.",
    },
    "audit": {
        "emoji": "⚖️",
        "title": "Audit My Bet",
        "subtitle": "Enter your win estimate + price and get a PLAY / PASS verdict with sizing.",
        "tip": "<strong>How it works:</strong> EV = (P_win × profit) − (P_loss × stake). "
               "We bake in the platform fee and surface a Kelly allocation so you size sanely.",
    },
    "hype": {
        "emoji": "📣",
        "title": "Hype vs Reality",
        "subtitle": "Compare crowd sentiment to the math — fade narratives when divergence is big.",
        "tip": "<strong>Divergence ≥ 20%</strong> flags a potential narrative bubble. "
               "Crowd over-bullish? Consider the other side.",
    },
    "arb": {
        "emoji": "💰",
        "title": "Risk-Free Cross-Book Arbs",
        "subtitle": "Pair Polymarket vs Kalshi — lock guaranteed profit when combined cost is below $1.00.",
        "tip": "<strong>The recipe:</strong> Buy YES on one book, NO on the other for the same outcome. "
               "If the costs add to less than $1.00, you've locked an arbitrage.",
    },
    "ledger": {
        "emoji": "📒",
        "title": "The Ledger",
        "subtitle": "Live P&L, win-rate, and a daily heatmap across all connected books.",
        "tip": "<strong>Connect to track.</strong> Add Kalshi and Polymarket API keys to sync fills automatically — see the setup panel below.",
    },
}


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
            "⚖️ Audit Bet",
            "📣 Hype vs Reality",
            "💰 Arbs",
            "📒 Ledger",
        ]
    )

    with tab_plays:
        h = _TAB_HEADERS["plays"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_top_value_plays()

    with tab_explore:
        h = _TAB_HEADERS["explore"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_explore_hub()

    with tab_audit:
        h = _TAB_HEADERS["audit"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_audit_my_bet()

    with tab_hype:
        h = _TAB_HEADERS["hype"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_hype_vs_reality()

    with tab_arb:
        h = _TAB_HEADERS["arb"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_risk_free_arbs()

    with tab_ledger:
        h = _TAB_HEADERS["ledger"]
        _render_tab_head(h["emoji"], h["title"], h["subtitle"], h["tip"])
        render_ledger()


main()
