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
                --pq-bg: #0a0e17;
                --pq-bg-elev: #111827;
                --pq-surface: #151c2c;
                --pq-surface-2: #1c2436;
                --pq-border: #243049;
                --pq-border-soft: #1c2336;
                --pq-text: #e6edf3;
                --pq-text-muted: #8b95a7;
                --pq-text-dim: #5d6679;
                --pq-accent: #3b82f6;
                --pq-accent-2: #60a5fa;
                --pq-success: #22c55e;
                --pq-success-2: #16a34a;
                --pq-warn: #f59e0b;
                --pq-danger: #ef4444;
                --pq-violet: #8b5cf6;
                --pq-pink: #ec4899;
                --pq-glow-accent: 0 0 0 1px rgba(59,130,246,0.35), 0 8px 28px -8px rgba(59,130,246,0.45);
                --pq-glow-success: 0 0 0 1px rgba(34,197,94,0.35), 0 8px 28px -8px rgba(34,197,94,0.45);
                --pq-radius: 14px;
            }

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            .stApp {
                background:
                    radial-gradient(1100px 600px at 8% -10%, rgba(59,130,246,0.10), transparent 60%),
                    radial-gradient(900px 500px at 100% 0%, rgba(139,92,246,0.08), transparent 60%),
                    linear-gradient(180deg, #0a0e17 0%, #0a0e17 100%);
                color: var(--pq-text);
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
                font-feature-settings: 'cv11','ss01','ss03';
                -webkit-font-smoothing: antialiased;
            }

            .block-container {
                padding: 0.65rem 1rem 2.5rem !important;
                max-width: 1240px !important;
            }

            /* Improve default Streamlit element rhythm */
            h1, h2, h3, h4, h5 { letter-spacing: -0.015em; }
            code, kbd, .mono { font-family: 'JetBrains Mono', ui-monospace, monospace; }
            ::selection { background: rgba(59,130,246,0.35); color: #fff; }

            /* Hero header — slim, sticky, with gradient accent */
            .pq-hero-shell {
                background:
                    linear-gradient(135deg, rgba(59,130,246,0.10), rgba(139,92,246,0.06) 60%, transparent),
                    var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 18px;
                padding: 0.85rem 1.1rem;
                margin: 0 0 0.85rem;
                box-shadow: 0 1px 0 rgba(255,255,255,0.04) inset, 0 16px 40px -28px rgba(0,0,0,0.8);
            }
            .pq-hero-row {
                display: flex; align-items: center; justify-content: space-between;
                gap: 0.85rem; flex-wrap: wrap;
            }
            .pq-hero-brand {
                display: flex; align-items: center; gap: 0.6rem;
            }
            .pq-hero-logo {
                width: 34px; height: 34px; border-radius: 10px;
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-violet));
                display: inline-flex; align-items: center; justify-content: center;
                color: #fff; font-weight: 900; font-size: 1rem;
                box-shadow: 0 6px 18px -6px rgba(59,130,246,0.6);
            }
            .pq-hero-title {
                font-size: 1.05rem; font-weight: 800; letter-spacing: -0.02em;
                color: #fff; margin: 0; line-height: 1;
            }
            .pq-hero-sub {
                font-size: 0.72rem; font-weight: 500;
                color: var(--pq-text-muted); margin-top: 0.18rem;
            }
            .pq-hero-meta {
                display: flex; align-items: center; gap: 0.4rem;
                flex-wrap: wrap;
            }
            .pq-chip {
                display: inline-flex; align-items: center; gap: 0.35rem;
                font-size: 0.7rem; font-weight: 700;
                padding: 0.32rem 0.6rem; border-radius: 999px;
                background: rgba(255,255,255,0.04);
                border: 1px solid var(--pq-border);
                color: var(--pq-text-muted);
            }
            .pq-chip.live::before {
                content: ""; width: 6px; height: 6px; border-radius: 50%;
                background: var(--pq-success);
                box-shadow: 0 0 0 0 rgba(34,197,94,0.7);
                animation: pq-pulse 1.8s infinite;
            }
            .pq-chip.build { color: var(--pq-accent-2); border-color: rgba(59,130,246,0.35); background: rgba(59,130,246,0.10); }
            .pq-chip.muted { color: var(--pq-text-muted); }
            .pq-chip code { color: var(--pq-accent-2); font-size: 0.7rem; }
            @keyframes pq-pulse {
                0% { box-shadow: 0 0 0 0 rgba(34,197,94,0.55); }
                70% { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
                100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
            }

            /* Top bar (legacy slot kept for back-compat) */
            .pq-topbar {
                display: none; /* superseded by .pq-hero-shell */
            }
            .pq-topbar-brand { font-weight: 800; color: #fff; }
            .pq-topbar-meta { color: var(--pq-text-muted); font-size: 0.72rem; }
            .pq-build-tag { color: var(--pq-accent-2); font-weight: 700; }

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

            /* Legacy hero (unused) */
            .pq-hero { display: none; }
            .pq-hero h1 { margin: 0; }
            .pq-hero p { margin: 0; }

            /* Tabs — segmented, modern */
            .stTabs [data-baseweb="tab-list"] {
                gap: 4px;
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 6px;
                margin-bottom: 0.5rem;
            }
            .stTabs [data-baseweb="tab"] {
                background: transparent;
                color: var(--pq-text-muted);
                font-weight: 600;
                font-size: 0.82rem;
                padding: 8px 14px !important;
                border-radius: 10px !important;
                border: none !important;
                transition: background 120ms ease, color 120ms ease, transform 120ms ease;
            }
            .stTabs [data-baseweb="tab"]:hover {
                color: #fff;
                background: rgba(255,255,255,0.04);
            }
            .stTabs [aria-selected="true"] {
                color: #fff !important;
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-violet)) !important;
                border-bottom: none !important;
                box-shadow: 0 6px 18px -8px rgba(59,130,246,0.55);
            }
            .stTabs [data-baseweb="tab-highlight"] { display: none !important; }
            .stTabs [data-baseweb="tab-border"] { display: none !important; }

            /* Cards */
            .pq-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1rem 1.1rem;
                margin-bottom: 0.7rem;
                transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
            }
            .pq-card:hover {
                border-color: rgba(59,130,246,0.35);
            }
            .pq-card-compound {
                border-color: rgba(34,197,94,0.45);
                background: linear-gradient(135deg, rgba(34,197,94,0.12), var(--pq-surface) 60%);
                box-shadow: 0 0 0 1px rgba(34,197,94,0.18), 0 18px 38px -22px rgba(34,197,94,0.45);
            }
            .pq-card-title {
                font-size: 0.95rem; font-weight: 700;
                color: var(--pq-text); line-height: 1.35;
                margin: 0 0 0.55rem;
            }
            .pq-card-row {
                display: flex; flex-wrap: wrap;
                gap: 0.5rem; align-items: center;
            }

            /* Badges */
            .pq-badge {
                display: inline-flex; align-items: center; gap: 0.3rem;
                padding: 0.25rem 0.6rem;
                border-radius: 999px;
                font-size: 0.7rem; font-weight: 700;
                letter-spacing: 0.02em; white-space: nowrap;
            }
            .pq-badge-green  { background: rgba(34,197,94,0.16); color: #4ade80; border: 1px solid rgba(34,197,94,0.4); }
            .pq-badge-blue   { background: rgba(59,130,246,0.14); color: #60a5fa; border: 1px solid rgba(59,130,246,0.4); }
            .pq-badge-grey   { background: rgba(255,255,255,0.04); color: var(--pq-text-muted); border: 1px solid var(--pq-border); }
            .pq-badge-red    { background: rgba(239,68,68,0.14); color: #f87171; border: 1px solid rgba(239,68,68,0.4); }
            .pq-badge-violet { background: rgba(139,92,246,0.14); color: #a78bfa; border: 1px solid rgba(139,92,246,0.4); }
            .pq-badge-amber  { background: rgba(245,158,11,0.14); color: #fbbf24; border: 1px solid rgba(245,158,11,0.4); }

            .pq-stat { font-size: 0.78rem; color: var(--pq-text-muted); }
            .pq-stat strong { color: var(--pq-text); font-weight: 700; }

            /* Verdict containers */
            .pq-verdict-play {
                background: linear-gradient(135deg, rgba(34,197,94,0.22), rgba(34,197,94,0.06));
                border: 1px solid rgba(34,197,94,0.55);
                border-radius: 16px;
                padding: 1.3rem 1.4rem;
                margin-top: 1rem;
                box-shadow: var(--pq-glow-success);
            }
            .pq-verdict-play h2 {
                margin: 0 0 0.35rem;
                font-size: 1.35rem; font-weight: 800;
                color: #4ade80;
            }
            .pq-verdict-play p { margin: 0; font-size: 0.95rem; color: #cbd5e1; line-height: 1.5; }
            .pq-verdict-pass {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1.3rem 1.4rem;
                margin-top: 1rem;
            }
            .pq-verdict-pass h2 { margin: 0 0 0.35rem; font-size: 1.2rem; font-weight: 800; color: var(--pq-text-muted); }
            .pq-verdict-pass p { margin: 0; font-size: 0.88rem; color: var(--pq-text-dim); }

            /* Arb split */
            .pq-split {
                display: grid; grid-template-columns: 1fr 1fr;
                gap: 0.75rem; margin: 0.75rem 0;
            }
            @media (max-width: 640px) { .pq-split { grid-template-columns: 1fr; } }
            .pq-split-side {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.9rem;
                text-align: center;
            }
            .pq-split-side .venue {
                font-size: 0.65rem; font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.1em;
                margin-bottom: 0.4rem;
            }
            .pq-split-side .leg {
                font-size: 1.02rem; font-weight: 800;
                color: var(--pq-accent-2);
            }
            .pq-arb-banner {
                background: linear-gradient(90deg, rgba(34,197,94,0.22), rgba(34,197,94,0.06));
                border: 1px solid rgba(34,197,94,0.45);
                border-radius: 14px;
                padding: 1rem 1.2rem;
                text-align: center;
                margin-top: 0.7rem;
                box-shadow: var(--pq-glow-success);
            }
            .pq-arb-banner h3 { margin: 0 0 0.25rem; color: #4ade80; font-size: 1.05rem; font-weight: 800; }
            .pq-arb-banner p  { margin: 0; color: #cbd5e1; font-size: 0.9rem; }

            /* Warning banner */
            .pq-trap-banner {
                background: linear-gradient(135deg, rgba(239,68,68,0.18), rgba(245,158,11,0.06));
                border: 1px solid rgba(239,68,68,0.5);
                border-radius: 14px;
                padding: 1.1rem 1.2rem;
                margin-top: 0.75rem;
            }
            .pq-trap-banner h3 { margin: 0 0 0.4rem; color: #f87171; font-size: 1rem; font-weight: 800; }
            .pq-trap-banner p  { margin: 0; color: #cbd5e1; font-size: 0.88rem; line-height: 1.45; }

            /* Input card */
            .pq-input-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.95rem 1.1rem 0.3rem;
                margin-bottom: 0.85rem;
            }

            /* Streamlit widgets — polished surfaces */
            [data-testid="stMetric"] {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.85rem 0.95rem;
                transition: border-color 160ms ease, transform 160ms ease;
            }
            [data-testid="stMetric"]:hover { border-color: rgba(59,130,246,0.45); }
            [data-testid="stMetricLabel"] p {
                color: var(--pq-text-muted) !important;
                font-size: 0.7rem !important;
                font-weight: 700 !important;
                text-transform: uppercase;
                letter-spacing: 0.08em;
            }
            [data-testid="stMetricValue"] {
                font-weight: 800 !important;
                color: #fff !important;
            }
            [data-testid="stDataFrame"] {
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                overflow: hidden;
            }
            .stSlider label, .stNumberInput label, .stSelectbox label, .stTextInput label {
                font-weight: 600 !important;
                font-size: 0.82rem !important;
                color: var(--pq-text-muted) !important;
            }
            .stNumberInput input, .stTextInput input {
                background: var(--pq-bg-elev) !important;
                border: 1px solid var(--pq-border) !important;
                color: var(--pq-text) !important;
                border-radius: 10px !important;
            }
            .stSelectbox [data-baseweb="select"] > div {
                background: var(--pq-bg-elev) !important;
                border: 1px solid var(--pq-border) !important;
                border-radius: 10px !important;
            }
            hr { border-color: var(--pq-border-soft); margin: 0.9rem 0; }

            /* Section labels & picker */
            .pq-section-label {
                font-size: 0.7rem; font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.12em;
                margin: 1rem 0 0.55rem;
            }
            .pq-pick-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.65rem 0.85rem;
                margin-bottom: 0.3rem;
                transition: border-color 160ms ease, background 160ms ease;
            }
            .pq-pick-card:hover { border-color: rgba(59,130,246,0.4); }
            .pq-pick-selected {
                border-color: var(--pq-accent) !important;
                background: rgba(59,130,246,0.10) !important;
                box-shadow: 0 0 0 1px rgba(59,130,246,0.3);
            }
            .pq-pick-title { display: block; font-size: 0.86rem; font-weight: 600; color: var(--pq-text); line-height: 1.35; }
            .pq-pick-meta  { display: block; font-size: 0.72rem; color: var(--pq-accent-2); font-weight: 700; margin-top: 0.18rem; }
            .pq-page-indicator { text-align: center; font-size: 0.75rem; color: var(--pq-text-muted); margin: 0.35rem 0 0; }
            .pq-selected-banner {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.7rem 0.85rem;
                font-size: 0.8rem;
                color: #cbd5e1;
                line-height: 1.4;
                margin: 0.5rem 0 0.75rem;
            }
            .pq-odds-bar {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.6rem 0.8rem 0.4rem;
                margin-bottom: 0.7rem;
            }

            /* Tactile buttons */
            .stButton > button {
                border-radius: 12px !important;
                font-weight: 700 !important;
                min-height: 2.45rem;
                font-size: 0.82rem !important;
                transition: transform 120ms ease, box-shadow 160ms ease, background 160ms ease, border-color 160ms ease;
            }
            .stButton > button:hover { transform: translateY(-1px); }
            .stButton > button[kind="secondary"] {
                background: var(--pq-surface-2) !important;
                border: 1px solid var(--pq-border) !important;
                color: #e5e7eb !important;
            }
            .stButton > button[kind="secondary"]:hover {
                border-color: var(--pq-accent) !important;
                color: #fff !important;
            }
            .stButton > button[kind="primary"] {
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-violet)) !important;
                border: 1px solid transparent !important;
                box-shadow: 0 8px 22px -10px rgba(59,130,246,0.7);
                color: #fff !important;
            }
            .stButton > button[kind="primary"]:hover {
                box-shadow: 0 14px 28px -10px rgba(59,130,246,0.85);
            }

            /* Segmented control polish */
            [data-testid="stSegmentedControl"] {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 4px;
            }
            [data-testid="stSegmentedControl"] button[aria-pressed="true"] {
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-violet)) !important;
                color: #fff !important;
                border-radius: 8px !important;
            }

            /* Expander surface */
            [data-testid="stExpander"] {
                background: var(--pq-surface) !important;
                border: 1px solid var(--pq-border) !important;
                border-radius: 14px !important;
                margin-bottom: 0.6rem;
            }
            [data-testid="stExpander"] summary {
                font-weight: 700 !important;
                color: var(--pq-text) !important;
            }

            /* Toasts / alerts */
            div[data-testid="stAlert"] {
                border-radius: 12px !important;
                border-width: 1px !important;
            }

            /* Pikkit-style explore feed */
            .pq-search-hero {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.7rem 0.9rem;
                margin-bottom: 0.6rem;
            }
            .pq-feed-row {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.85rem 0.95rem;
                margin-bottom: 0.5rem;
                transition: border-color 160ms ease, transform 160ms ease;
            }
            .pq-feed-row:hover {
                border-color: rgba(59,130,246,0.4);
                transform: translateY(-1px);
            }
            .pq-feed-meta {
                display: block;
                font-size: 0.62rem;
                font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.1em;
                margin-bottom: 0.3rem;
            }
            .pq-feed-title { display: block; font-size: 0.9rem; font-weight: 700; color: var(--pq-text); line-height: 1.35; }
            .pq-feed-event { display: block; font-size: 0.72rem; color: var(--pq-text-dim); margin-top: 0.22rem; }
            .pq-odd-pill {
                display: block; text-align: center;
                padding: 0.55rem 0.4rem; border-radius: 10px;
                font-weight: 800; font-size: 0.95rem;
            }
            .pq-odd-yes {
                background: rgba(59,130,246,0.12);
                color: var(--pq-accent-2);
                border: 1px solid rgba(59,130,246,0.35);
            }
            .pq-odd-no {
                background: rgba(255,255,255,0.04);
                color: var(--pq-text);
                border: 1px solid var(--pq-border);
            }

            /* Phase 1 — tactile value cards */
            .pq-value-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1.1rem 1.2rem;
                margin-bottom: 0.75rem;
                transition: transform 160ms ease, border-color 160ms ease, box-shadow 200ms ease;
            }
            .pq-value-card:hover {
                transform: translateY(-2px);
                border-color: rgba(59,130,246,0.35);
            }
            .pq-value-card-hot {
                border-color: rgba(34,197,94,0.45);
                box-shadow: 0 0 0 1px rgba(34,197,94,0.18), 0 18px 38px -22px rgba(34,197,94,0.45);
            }
            .pq-event-name {
                font-size: 0.98rem; font-weight: 800;
                color: var(--pq-text);
                margin: 0 0 0.7rem;
                line-height: 1.35;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(135deg, var(--pq-accent), var(--pq-violet));
                color: #fff;
                font-weight: 800;
                font-size: 0.82rem;
                padding: 0.5rem 0.9rem;
                border-radius: 999px;
                margin-bottom: 0.6rem;
                letter-spacing: 0.02em;
                box-shadow: 0 8px 22px -10px rgba(59,130,246,0.7);
            }
            .pq-ev-badge {
                display: inline-block;
                background: rgba(34,197,94,0.18);
                color: #4ade80;
                border: 1px solid rgba(34,197,94,0.45);
                font-weight: 800;
                font-size: 0.8rem;
                padding: 0.32rem 0.7rem;
                border-radius: 999px;
            }
            .pq-metric-row {
                display: flex; gap: 1.25rem; flex-wrap: wrap;
                font-size: 0.8rem; color: var(--pq-text-muted);
            }
            .pq-metric-row strong { color: var(--pq-text); }

            /* Full-width audit banner */
            .pq-banner-play {
                background: linear-gradient(90deg, rgba(34,197,94,0.28), rgba(34,197,94,0.06));
                border: 1px solid rgba(34,197,94,0.55);
                border-radius: 14px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.4rem;
                font-weight: 900;
                color: #4ade80;
                margin-top: 1rem;
                letter-spacing: 0.04em;
                box-shadow: var(--pq-glow-success);
            }
            .pq-banner-pass {
                background: rgba(239,68,68,0.10);
                border: 1px solid rgba(239,68,68,0.35);
                border-radius: 14px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.3rem;
                font-weight: 900;
                color: #f87171;
                margin-top: 1rem;
            }

            /* Hype vs Reality */
            .pq-hype-col {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 1.1rem;
                text-align: center;
            }
            .pq-hype-val { font-size: 2.1rem; font-weight: 900; color: #fff; }
            .pq-bubble-badge {
                background: linear-gradient(90deg, rgba(245,158,11,0.30), rgba(239,68,68,0.18));
                border: 1px solid rgba(245,158,11,0.55);
                color: #fbbf24;
                font-weight: 900;
                font-size: 0.95rem;
                padding: 1rem 1.1rem;
                border-radius: 14px;
                text-align: center;
                margin-top: 0.85rem;
                box-shadow: 0 0 0 1px rgba(245,158,11,0.15), 0 16px 32px -22px rgba(245,158,11,0.5);
            }

            /* Arb recipe */
            .pq-recipe {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 1.05rem 1.2rem;
                margin: 0.55rem 0;
            }
            .pq-recipe-step { font-size: 0.92rem; color: #cbd5e1; margin: 0.5rem 0; line-height: 1.55; }
            .pq-recipe-step strong { color: var(--pq-accent-2); }
            .pq-lock-banner {
                background: linear-gradient(90deg, rgba(34,197,94,0.25), rgba(34,197,94,0.05));
                border: 1px solid rgba(34,197,94,0.55);
                border-radius: 14px;
                padding: 1rem;
                text-align: center;
                font-size: 1.1rem;
                font-weight: 800;
                color: #4ade80;
                margin-top: 0.75rem;
                box-shadow: var(--pq-glow-success);
            }

            /* Cross-book arb comparison */
            .pq-arb-compare {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1.05rem 1.15rem;
                margin: 0.85rem 0 1rem;
            }
            .pq-arb-grid {
                display: grid; grid-template-columns: 1fr 1fr;
                gap: 0.7rem;
            }
            @media (max-width: 640px) { .pq-arb-grid { grid-template-columns: 1fr; } }
            .pq-book-card {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.9rem;
            }
            .pq-book-header {
                font-size: 0.66rem; font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.1em;
                margin-bottom: 0.4rem;
            }
            .pq-book-title {
                font-size: 0.84rem; font-weight: 700;
                color: var(--pq-text); line-height: 1.35;
                margin-bottom: 0.65rem; min-height: 2.2rem;
            }
            .pq-odd-row {
                display: flex; justify-content: space-between; align-items: center;
                padding: 0.5rem 0.6rem; border-radius: 10px;
                margin-bottom: 0.4rem;
                font-size: 0.8rem; font-weight: 700;
            }
            .pq-odd-row.yes {
                background: rgba(59,130,246,0.14);
                border: 1px solid rgba(59,130,246,0.4);
                color: var(--pq-accent-2);
            }
            .pq-odd-row.no {
                background: rgba(255,255,255,0.04);
                border: 1px solid var(--pq-border);
                color: #cbd5e1;
            }
            .pq-odd-row .pq-odd-val { font-weight: 800; color: var(--pq-text); font-size: 0.78rem; }
            .pq-strategy-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1.05rem 1.15rem;
                margin: 0.7rem 0;
                transition: border-color 160ms ease;
            }
            .pq-strategy-card:hover { border-color: rgba(59,130,246,0.4); }
            .pq-strategy-card.pq-strategy-live {
                border-color: rgba(34,197,94,0.55);
                box-shadow: var(--pq-glow-success);
            }
            .pq-strategy-head {
                display: flex; justify-content: space-between; align-items: center;
                margin-bottom: 0.7rem; flex-wrap: wrap; gap: 0.35rem;
            }
            .pq-strategy-title { font-size: 0.95rem; font-weight: 800; color: var(--pq-text); margin: 0; }
            .pq-strategy-badge {
                font-size: 0.66rem; font-weight: 800;
                padding: 0.28rem 0.6rem; border-radius: 999px;
                text-transform: uppercase; letter-spacing: 0.06em;
            }
            .pq-strategy-badge.live { background: rgba(34,197,94,0.18); color: #4ade80; border: 1px solid rgba(34,197,94,0.45); }
            .pq-strategy-badge.dead { background: rgba(255,255,255,0.04); color: var(--pq-text-muted); border: 1px solid var(--pq-border); }
            .pq-strategy-metrics {
                display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 0.55rem; margin-top: 0.7rem;
            }
            @media (max-width: 480px) { .pq-strategy-metrics { grid-template-columns: 1fr; } }
            .pq-metric-box {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.65rem 0.75rem;
                text-align: center;
            }
            .pq-metric-box .lbl {
                display: block; font-size: 0.62rem; font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase; letter-spacing: 0.08em;
            }
            .pq-metric-box .val {
                display: block; font-size: 1rem; font-weight: 800;
                color: #fff; margin-top: 0.2rem;
            }
            .pq-metric-box .val.green { color: #4ade80; }
            .pq-metric-box .val.red   { color: #f87171; }
            .pq-arb-detail {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.9rem;
                margin-top: 0.75rem;
            }
            .pq-arb-detail-title {
                margin: 0 0 0.55rem;
                color: var(--pq-text);
                font-size: 0.74rem; font-weight: 800;
                text-transform: uppercase; letter-spacing: 0.1em;
            }
            .pq-arb-ticket-row {
                display: grid;
                grid-template-columns: 1.25fr 0.75fr 0.8fr 0.9fr;
                gap: 0.5rem;
                align-items: center;
                padding: 0.55rem 0;
                border-top: 1px solid var(--pq-border-soft);
                font-size: 0.8rem;
                color: #cbd5e1;
            }
            .pq-arb-ticket-row.header {
                border-top: 0; padding-top: 0;
                color: var(--pq-text-muted);
                font-size: 0.62rem; font-weight: 800;
                text-transform: uppercase; letter-spacing: 0.08em;
            }
            .pq-arb-ticket-row strong { color: var(--pq-text); font-weight: 800; }
            .pq-arb-ticket-row .cash { color: var(--pq-accent-2); font-weight: 800; text-align: right; }
            .pq-arb-explain { margin: 0.7rem 0 0; color: #cbd5e1; font-size: 0.84rem; line-height: 1.55; }
            .pq-arb-explain strong { color: var(--pq-text); }
            .pq-arb-warning {
                background: rgba(239,68,68,0.10);
                border: 1px solid rgba(239,68,68,0.45);
                border-radius: 12px;
                color: #fecaca;
                font-size: 0.82rem;
                line-height: 1.5;
                margin-top: 0.75rem;
                padding: 0.8rem 0.85rem;
            }
            .pq-arb-spotlight {
                background:
                    linear-gradient(180deg, rgba(59,130,246,0.16), rgba(10,14,23,0.96)),
                    var(--pq-surface);
                border: 1px solid rgba(59,130,246,0.45);
                border-radius: 18px;
                box-shadow: var(--pq-glow-accent);
                margin: 0.85rem 0 1.1rem;
                padding: 1.1rem 1.15rem;
            }
            .pq-arb-spotlight.live {
                background:
                    linear-gradient(180deg, rgba(34,197,94,0.18), rgba(10,14,23,0.96)),
                    var(--pq-surface);
                border-color: rgba(34,197,94,0.55);
                box-shadow: var(--pq-glow-success);
            }
            .pq-arb-spotlight.dead {
                background:
                    linear-gradient(180deg, rgba(239,68,68,0.14), rgba(10,14,23,0.96)),
                    var(--pq-surface);
                border-color: rgba(239,68,68,0.5);
                box-shadow: 0 0 0 1px rgba(239,68,68,0.25), 0 12px 32px -16px rgba(239,68,68,0.45);
            }
            .pq-arb-spotlight-kicker {
                color: var(--pq-text-muted);
                font-size: 0.66rem; font-weight: 900;
                letter-spacing: 0.1em;
                margin: 0 0 0.3rem;
                text-transform: uppercase;
            }
            .pq-arb-spotlight-title {
                color: #fff;
                font-size: 1.1rem; font-weight: 900;
                letter-spacing: -0.02em;
                line-height: 1.25;
                margin: 0 0 0.75rem;
            }
            .pq-arb-action-list { display: grid; gap: 0.55rem; margin: 0.75rem 0; }
            .pq-arb-action {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.85rem;
            }
            .pq-arb-action .step {
                color: var(--pq-text-muted);
                display: block;
                font-size: 0.64rem; font-weight: 900;
                letter-spacing: 0.1em;
                text-transform: uppercase;
            }
            .pq-arb-action .take { color: #fff; display: block; font-size: 0.95rem; font-weight: 900; margin-top: 0.2rem; }
            .pq-arb-action .meta { color: var(--pq-accent-2); display: block; font-size: 0.78rem; font-weight: 700; margin-top: 0.22rem; }
            .pq-arb-spotlight-note { color: #cbd5e1; font-size: 0.84rem; line-height: 1.55; margin: 0.7rem 0 0; }
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
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.75rem 0.9rem;
                margin-bottom: 0.4rem;
            }
            .pq-suggest-score {
                display: inline-block;
                font-size: 0.65rem; font-weight: 800;
                color: var(--pq-accent-2);
                background: rgba(59,130,246,0.14);
                border: 1px solid rgba(59,130,246,0.4);
                border-radius: 999px;
                padding: 0.18rem 0.5rem;
                margin-bottom: 0.4rem;
            }
            .pq-suggest-title { display: block; font-size: 0.86rem; font-weight: 700; color: var(--pq-text); line-height: 1.35; }
            .pq-suggest-meta  { display: block; font-size: 0.72rem; color: var(--pq-accent-2); font-weight: 600; margin-top: 0.22rem; }

            /* Performance calendar */
            .pq-perf-calendar {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1rem 1.05rem 1.1rem;
                margin: 0.75rem 0 1rem;
            }
            .pq-perf-cal-header {
                display: flex; justify-content: space-between; align-items: baseline;
                margin-bottom: 0.7rem; flex-wrap: wrap; gap: 0.4rem;
            }
            .pq-perf-cal-title { font-size: 0.98rem; font-weight: 800; color: #fff; letter-spacing: -0.02em; }
            .pq-perf-cal-sub   { font-size: 0.72rem; font-weight: 600; color: var(--pq-text-muted); }
            .pq-perf-cal-month-pnl { font-size: 0.85rem; font-weight: 800; }
            .pq-perf-cal-month-pnl.pos { color: #4ade80; }
            .pq-perf-cal-month-pnl.neg { color: #f87171; }
            .pq-perf-cal-month-pnl.flat { color: var(--pq-text-muted); }
            .pq-perf-cal-grid {
                display: grid; grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 6px;
            }
            .pq-perf-cal-head {
                text-align: center;
                font-size: 0.62rem; font-weight: 800;
                color: var(--pq-text-dim);
                text-transform: uppercase; letter-spacing: 0.08em;
                padding: 0.2rem 0 0.4rem;
            }
            .pq-perf-cal-cell {
                min-height: 62px;
                border-radius: 10px;
                border: 1px solid var(--pq-border-soft);
                background: var(--pq-bg-elev);
                padding: 0.4rem 0.32rem 0.32rem;
                display: flex; flex-direction: column;
                justify-content: space-between; align-items: stretch;
                transition: transform 140ms ease, border-color 160ms ease;
            }
            .pq-perf-cal-cell:hover { transform: translateY(-1px); border-color: rgba(59,130,246,0.35); }
            .pq-perf-cal-cell.pq-perf-empty { background: transparent; border-color: transparent; min-height: 0; padding: 0; }
            .pq-perf-cal-cell.pq-perf-today { box-shadow: 0 0 0 2px var(--pq-accent); }
            .pq-perf-cal-cell.pq-perf-win   { background: rgba(34,197,94,0.16); border-color: rgba(34,197,94,0.45); }
            .pq-perf-cal-cell.pq-perf-loss  { background: rgba(239,68,68,0.14); border-color: rgba(239,68,68,0.4); }
            .pq-perf-cal-cell.pq-perf-flat  { background: var(--pq-surface-2); border-color: var(--pq-border); }
            .pq-perf-cal-day { font-size: 0.62rem; font-weight: 700; color: var(--pq-text-muted); line-height: 1; }
            .pq-perf-cal-pnl { font-size: 0.74rem; font-weight: 800; text-align: center; line-height: 1.1; margin-top: 0.2rem; }
            .pq-perf-cal-pnl.pos { color: #4ade80; }
            .pq-perf-cal-pnl.neg { color: #f87171; }
            .pq-perf-cal-pnl.flat { color: #cbd5e1; }
            .pq-perf-cal-count { font-size: 0.58rem; font-weight: 600; color: var(--pq-text-dim); text-align: center; margin-top: 0.1rem; }

            /* Ledger calendar flexbox (legacy) */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid { display: flex; flex-wrap: wrap; gap: 4px; }
            .pq-cal-head {
                flex: 1 0 calc(14.28% - 4px); min-width: 0;
                text-align: center; font-size: 0.65rem; font-weight: 700;
                color: var(--pq-text-muted); padding: 0.25rem 0;
            }
            .pq-cal-cell {
                flex: 1 0 calc(14.28% - 4px); min-width: 0;
                aspect-ratio: 1; border-radius: 10px;
                border: 1px solid var(--pq-border);
                position: relative; display: flex;
                align-items: center; justify-content: center;
            }
            .pq-cal-day { position: absolute; top: 4px; left: 6px; font-size: 0.62rem; color: var(--pq-text-muted); font-weight: 600; }
            .pq-cal-neutral { background: var(--pq-surface); }
            .pq-cal-win  { background: rgba(34,197,94,0.20); border-color: rgba(34,197,94,0.4); }
            .pq-cal-loss { background: rgba(239,68,68,0.16); border-color: rgba(239,68,68,0.35); }
            .pq-cal-pnl { font-size: 0.72rem; font-weight: 800; }
            .pq-cal-pnl.pos { color: #4ade80; }
            .pq-cal-pnl.neg { color: #f87171; }
            .pq-cal-dash { color: var(--pq-text-dim); font-size: 0.85rem; }

            /* Elite value plays (SOP) */
            .pq-value-card-elite {
                border: 1px solid rgba(34,197,94,0.55) !important;
                box-shadow: var(--pq-glow-success);
            }
            .pq-rank-badge {
                display: inline-block;
                background: rgba(59,130,246,0.16);
                color: var(--pq-accent-2);
                border: 1px solid rgba(59,130,246,0.4);
                font-weight: 800; font-size: 0.72rem;
                padding: 0.3rem 0.7rem; border-radius: 999px;
                margin-bottom: 0.55rem; letter-spacing: 0.04em;
            }
            .pq-rank-badge-elite {
                background: rgba(34,197,94,0.22);
                color: #4ade80;
                border-color: rgba(34,197,94,0.5);
                font-size: 0.78rem;
            }

            /* Compact explore feed — single row on mobile */
            .pq-feed-compact { display: flex; align-items: center; justify-content: space-between; gap: 0.7rem; flex-wrap: wrap; }
            .pq-feed-body { flex: 1 1 200px; min-width: 0; }
            .pq-feed-odds { display: flex; gap: 0.4rem; flex-shrink: 0; }
            .pq-odd-pill.sm { padding: 0.4rem 0.55rem; font-size: 0.78rem; border-radius: 10px; white-space: nowrap; }

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

            /* === New: Overview / dashboard widgets === */
            .pq-page-header {
                display: flex; flex-direction: column; gap: 0.25rem;
                margin: 0.2rem 0 0.55rem;
            }
            .pq-page-eyebrow {
                color: var(--pq-accent-2);
                font-size: 0.7rem; font-weight: 800;
                letter-spacing: 0.16em; text-transform: uppercase;
            }
            .pq-page-title {
                color: #fff; font-size: 1.45rem; font-weight: 800;
                letter-spacing: -0.02em; margin: 0;
            }
            .pq-page-sub {
                color: var(--pq-text-muted);
                font-size: 0.88rem; line-height: 1.5;
                margin: 0; max-width: 720px;
            }
            .pq-kpi-grid {
                display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 0.7rem; margin: 0.4rem 0 0.85rem;
            }
            @media (max-width: 820px) { .pq-kpi-grid { grid-template-columns: repeat(2, 1fr); } }
            .pq-kpi {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 0.95rem 1.05rem;
                position: relative; overflow: hidden;
                transition: border-color 160ms ease, transform 160ms ease;
            }
            .pq-kpi:hover { transform: translateY(-2px); border-color: rgba(59,130,246,0.4); }
            .pq-kpi::after {
                content: ""; position: absolute; right: -28px; top: -28px;
                width: 90px; height: 90px; border-radius: 50%;
                background: radial-gradient(circle at center, rgba(59,130,246,0.25), transparent 70%);
                pointer-events: none;
            }
            .pq-kpi.success::after { background: radial-gradient(circle at center, rgba(34,197,94,0.28), transparent 70%); }
            .pq-kpi.warn::after    { background: radial-gradient(circle at center, rgba(245,158,11,0.28), transparent 70%); }
            .pq-kpi.violet::after  { background: radial-gradient(circle at center, rgba(139,92,246,0.28), transparent 70%); }
            .pq-kpi-label {
                display: block; font-size: 0.66rem;
                font-weight: 800; letter-spacing: 0.12em;
                text-transform: uppercase; color: var(--pq-text-muted);
            }
            .pq-kpi-value {
                display: block; font-size: 1.75rem; font-weight: 900;
                color: #fff; margin-top: 0.35rem; letter-spacing: -0.025em;
                line-height: 1.05;
            }
            .pq-kpi-trend {
                display: block; font-size: 0.74rem; font-weight: 700;
                margin-top: 0.35rem; color: var(--pq-text-muted);
            }
            .pq-kpi-trend.pos { color: #4ade80; }
            .pq-kpi-trend.neg { color: #f87171; }

            .pq-section-heading {
                display: flex; align-items: baseline; justify-content: space-between;
                gap: 0.6rem; margin: 1.2rem 0 0.55rem;
            }
            .pq-section-heading h3 {
                margin: 0; color: #fff; font-size: 1.05rem; font-weight: 800; letter-spacing: -0.015em;
            }
            .pq-section-heading .hint {
                color: var(--pq-text-muted); font-size: 0.75rem; font-weight: 600;
            }

            .pq-tour {
                background:
                    linear-gradient(135deg, rgba(139,92,246,0.10), rgba(59,130,246,0.06)),
                    var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 16px;
                padding: 1rem 1.15rem;
                margin: 0.4rem 0 0.85rem;
            }
            .pq-tour-grid {
                display: grid; grid-template-columns: repeat(3, 1fr);
                gap: 0.7rem; margin-top: 0.65rem;
            }
            @media (max-width: 760px) { .pq-tour-grid { grid-template-columns: 1fr; } }
            .pq-tour-card {
                background: var(--pq-bg-elev);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.85rem 0.95rem;
            }
            .pq-tour-step {
                color: var(--pq-accent-2); font-size: 0.66rem; font-weight: 800;
                letter-spacing: 0.12em; text-transform: uppercase;
            }
            .pq-tour-card h4 { margin: 0.35rem 0 0.35rem; color: #fff; font-size: 0.95rem; font-weight: 800; }
            .pq-tour-card p  { margin: 0; color: var(--pq-text-muted); font-size: 0.82rem; line-height: 1.5; }

            .pq-mini-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 0.9rem 1rem;
            }
            .pq-mini-card .lbl {
                font-size: 0.66rem; font-weight: 800;
                color: var(--pq-text-muted);
                letter-spacing: 0.1em; text-transform: uppercase;
            }
            .pq-mini-card .val { font-size: 1.05rem; font-weight: 800; color: #fff; margin-top: 0.25rem; }
            .pq-mini-card .sub { font-size: 0.74rem; color: var(--pq-text-muted); margin-top: 0.18rem; }

            /* Captions, links */
            .stCaption, [data-testid="stCaptionContainer"] { color: var(--pq-text-muted) !important; }
            a { color: var(--pq-accent-2); }
            a:hover { color: #fff; }
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
    _page_header(
        "Value Plays",
        "Today's sharpest edges",
        f"Strict elite tier — only contracts with model win-rate above {VALUE_PLAYS_WIN_MIN:.0f}% "
        f"and net EV ≥{VALUE_PLAYS_EV_EDGE_MIN:.0f}% (after platform fees). Capped at the top "
        f"{VALUE_PLAYS_MAX} sharpest opportunities so signal isn't drowned in noise.",
    )

    act_l, act_r = st.columns([1, 4])
    with act_l:
        if st.button("↻ Refresh markets", key="refresh_poly", use_container_width=True):
            fetch_polymarket_markets.clear()
            st.rerun()
    with act_r:
        st.caption("Markets are cached for 60s. Refresh to pull the latest book snapshot.")

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
            <div class="pq-card" style="text-align:center;padding:2rem 1.25rem;">
                <p class="pq-event-name" style="margin-bottom:0.5rem;">No action today</p>
                <p style="color:var(--pq-text-muted);font-size:0.95rem;line-height:1.5;margin:0;">
                    No mathematically viable anomalies detected right now. Markets refresh every minute —
                    try <strong>🔍 Markets</strong> to browse the wider universe, or check back shortly.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    summary = df["Net EV Edge %"].astype(float)
    _render_kpi_grid([
        {"label": "Elite plays found", "value": f"{len(df)}",
         "trend": "After SOP filters", "tone": "success"},
        {"label": "Sharpest edge", "value": f"+{summary.max():.2f}%",
         "trend": "Net EV after fees", "trend_cls": "pos", "tone": "accent"},
        {"label": "Average edge", "value": f"+{summary.mean():.2f}%",
         "trend": "Across surfaced plays", "tone": "violet"},
        {"label": "Combined volume",
         "value": f"${df['Volume'].astype(float).sum():,.0f}",
         "trend": "Polymarket reported", "tone": "warn"},
    ])

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

    _render_kpi_grid([
        {"label": "True probability", "value": f"{true_win_prob:.1f}%",
         "trend": "Your input", "tone": "accent"},
        {"label": "Quantitative edge", "value": edge_display,
         "trend": "EV yield on stake",
         "trend_cls": "pos" if ev_yield_pct >= 0 else "neg",
         "tone": "success" if ev_yield_pct >= 5 else ("warn" if ev_yield_pct > 0 else "violet")},
        {"label": "Projected EV", "value": f"${ev_dollars:+,.2f}",
         "trend": f"on ${stake:,.0f} stake",
         "trend_cls": "pos" if ev_dollars >= 0 else "neg", "tone": "warn"},
        {"label": "Recommended size",
         "value": f"{kelly_pct:.1f}% units",
         "trend": "Kelly fraction of bankroll", "tone": "violet"},
    ])

    if ev_yield_pct >= 5.0:
        st.markdown(
            '<div class="pq-verdict-play"><h2>✅ PLAY · Edge clears every gate</h2>'
            '<p>This line is mathematically priced wrong in your favor. Size within your Kelly '
            'allocation and execute if liquidity supports it.</p></div>',
            unsafe_allow_html=True,
        )
    elif ev_yield_pct > 0.0:
        st.markdown(
            '<div class="pq-card" style="border-color:rgba(245,158,11,0.45);">'
            '<h3 style="margin:0 0 .35rem;color:#fbbf24;">⚠️ MARGINAL · positive EV, below elite band</h3>'
            '<p style="margin:0;color:#cbd5e1;font-size:.9rem;line-height:1.5;">'
            'Edge exists but doesn\'t clear the 5% matured-advantage threshold. Size down, '
            'or wait for the line to move further in your favor.</p></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="pq-verdict-pass" style="border-color:rgba(239,68,68,0.45);">'
            '<h2 style="color:#f87171;">⛔ PASS · no edge</h2>'
            '<p style="color:#cbd5e1;">The offer price exceeds your model\'s fair value. '
            'Preserve bankroll and look for the next opportunity.</p></div>',
            unsafe_allow_html=True,
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
    _page_header(
        "Bet calculator",
        "Score any line in one click",
        "Drop in your true win-rate, the offered share price, and your stake. We compute expected value, "
        "Kelly sizing and a pass / play verdict against our quantitative gates.",
    )

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
    _page_header(
        "Sentiment",
        "Hype vs. reality",
        "Compare what the crowd is saying to what the math actually says. A gap above "
        f"±{DIVERGENCE_TRIGGER:.0f} pts often signals a narrative bubble (or a buying opportunity).",
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            '<div class="pq-hype-col">'
            '<p style="color:var(--pq-text-muted);font-size:0.72rem;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.08em;margin:0 0 0.4rem;">What people are saying</p>',
            unsafe_allow_html=True,
        )
        sentiment = st.slider("Social Sentiment", 0.0, 100.0, 50.0, 0.5,
                              label_visibility="collapsed", key="hype_sent")
        st.markdown(
            f'<p class="pq-hype-val">{sentiment:.0f}%</p>'
            '<p style="margin:0;color:var(--pq-text-muted);font-size:.78rem;">Social sentiment score</p>'
            '</div>',
            unsafe_allow_html=True,
        )
    with col2:
        st.markdown(
            '<div class="pq-hype-col">'
            '<p style="color:var(--pq-text-muted);font-size:0.72rem;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.08em;margin:0 0 0.4rem;">What the math says</p>',
            unsafe_allow_html=True,
        )
        implied_prob = st.slider("True Win", 0.0, 100.0, 50.0, 0.5,
                                 label_visibility="collapsed", key="hype_real")
        st.markdown(
            f'<p class="pq-hype-val">{implied_prob:.0f}%</p>'
            '<p style="margin:0;color:var(--pq-text-muted);font-size:.78rem;">Model implied win-rate</p>'
            '</div>',
            unsafe_allow_html=True,
        )

    delta = sentiment - implied_prob
    direction = "Crowd hotter than the math" if delta >= 0 else "Crowd colder than the math"
    delta_chip_tone = "pq-badge-amber" if abs(delta) >= DIVERGENCE_TRIGGER else "pq-badge-grey"
    st.markdown(
        f'<div class="pq-card" style="display:flex;align-items:center;justify-content:space-between;gap:.6rem;flex-wrap:wrap;">'
        f'<div><strong style="color:#fff;">Divergence</strong>'
        f' <span style="color:var(--pq-text-muted);font-size:.85rem;">· {html.escape(direction)}</span></div>'
        f'<span class="pq-badge {delta_chip_tone}">{delta:+.1f} pts</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if delta >= DIVERGENCE_TRIGGER:
        st.markdown(
            '<div class="pq-bubble-badge">🔥 Narrative bubble — consider fading the public</div>',
            unsafe_allow_html=True,
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.markdown(
            '<div class="pq-card pq-card-compound" style="text-align:center;">'
            '<span class="pq-badge pq-badge-blue">💎 Crowd too bearish — YES may be cheap</span>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="pq-card" style="text-align:center;">'
            '<span class="pq-badge pq-badge-grey">⚖️ Aligned — no narrative edge right now</span>'
            '</div>',
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
    _page_header(
        "Markets",
        "Browse every live contract",
        "Search and filter the unified Polymarket + Kalshi catalog. Tap any market to send it straight to "
        "the Arbs tab for cross-book pricing.",
    )
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
    _page_header(
        "Arbs",
        "Lock cross-book profit",
        "Pick one market on each exchange — we show exact contract sizing, cash needed per book, the "
        "guaranteed payout and a clear warning when prices haven't crossed into arb territory yet.",
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
    _page_header(
        "Performance ledger",
        "Your trades, settled and live",
        "Sync filled orders from Kalshi and Polymarket to see daily P&L, win/loss record, capital at risk, "
        "and a heatmap calendar of every settlement day.",
    )

    creds = _ledger_credentials()
    _render_api_keys_setup_panel(creds)

    if not creds["kalshi"] and not creds["polymarket"]:
        st.info("Connect at least one account, then tap **Sync fills** to populate your ledger.")

    act_l, act_r = st.columns([1, 4])
    with act_l:
        if st.button("↻ Sync fills", key="refresh_ledger", type="primary", use_container_width=True):
            fetch_unified_ledger.clear()
            st.rerun()
    with act_r:
        st.caption("Fills are cached for 2 minutes. Sync again to pull the latest.")

    ledger = fetch_unified_ledger()
    daily_net, wl_record, capital_at_risk = _ledger_kpis(ledger)

    total_pnl = (
        float(ledger.loc[ledger["Status"].isin(["WON", "LOST"]), "Net Return $"].sum())
        if not ledger.empty else 0.0
    )

    _render_kpi_grid([
        {"label": "Daily net P&L",
         "value": f"${daily_net:+,.2f}",
         "trend": "Settled today",
         "trend_cls": "pos" if daily_net >= 0 else "neg",
         "tone": "success" if daily_net >= 0 else "warn"},
        {"label": "Monthly record", "value": wl_record,
         "trend": "Wins / losses MTD", "tone": "accent"},
        {"label": "Capital at risk",
         "value": f"${capital_at_risk:,.2f}",
         "trend": "Open position stake", "tone": "violet"},
        {"label": "All-time P&L",
         "value": f"${total_pnl:+,.2f}",
         "trend": "Settled fills only",
         "trend_cls": "pos" if total_pnl >= 0 else "neg",
         "tone": "warn"},
    ])

    _render_performance_calendar(ledger)

    if ledger.empty:
        st.markdown(
            '<div class="pq-card" style="text-align:center;padding:1.6rem 1.1rem;">'
            '<p style="margin:0;color:#fff;font-weight:700;">No filled orders ingested yet.</p>'
            '<p style="margin:.35rem 0 0;color:var(--pq-text-muted);font-size:.85rem;line-height:1.5;">'
            'Connect your Kalshi or Polymarket API keys above, then hit <strong>Sync fills</strong>.'
            '</p></div>',
            unsafe_allow_html=True,
        )
        return

    # ---- Cumulative P&L chart ----
    settled = ledger[ledger["Status"].isin(["WON", "LOST"])].copy()
    if not settled.empty:
        settled["_d"] = pd.to_datetime(settled["Date"], errors="coerce")
        daily = (
            settled.dropna(subset=["_d"])
            .groupby(settled["_d"].dt.date)["Net Return $"].sum()
            .sort_index()
        )
        if not daily.empty:
            cum = daily.cumsum().rename("Cumulative P&L").to_frame()
            st.markdown(
                '<div class="pq-section-heading"><h3>📈 Equity curve</h3>'
                '<span class="hint">Cumulative settled P&amp;L across all books.</span></div>',
                unsafe_allow_html=True,
            )
            st.area_chart(cum, use_container_width=True, height=220, color="#3b82f6")

    st.markdown(
        '<div class="pq-section-heading"><h3>📒 Fill history</h3>'
        '<span class="hint">Every filled order from connected books.</span></div>',
        unsafe_allow_html=True,
    )

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
# Presentation helpers — page headers, KPIs, dashboard
# --------------------------------------------------------------------------- #


def _page_header(eyebrow: str, title: str, sub: str = "") -> None:
    """Modern eyebrow + title + sub layout used at the top of each tab."""
    sub_html = (
        f'<p class="pq-page-sub">{html.escape(sub)}</p>' if sub else ""
    )
    st.markdown(
        f"""
        <div class="pq-page-header">
            <span class="pq-page-eyebrow">{html.escape(eyebrow)}</span>
            <h2 class="pq-page-title">{html.escape(title)}</h2>
            {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_kpi_grid(items: list[dict[str, str]]) -> None:
    """Render up to 4 KPI tiles in a responsive grid.

    Each item: {label, value, trend, tone}  (tone ∈ accent|success|warn|violet)
    """
    if not items:
        return
    tile_html: list[str] = []
    for it in items:
        tone = it.get("tone", "accent")
        trend_raw = it.get("trend", "")
        trend_cls = it.get("trend_cls", "")
        trend_html = (
            f'<span class="pq-kpi-trend {html.escape(trend_cls)}">{html.escape(trend_raw)}</span>'
            if trend_raw else ""
        )
        tile_html.append(
            f"""
            <div class="pq-kpi {html.escape(tone)}">
                <span class="pq-kpi-label">{html.escape(it.get('label',''))}</span>
                <span class="pq-kpi-value">{html.escape(str(it.get('value','—')))}</span>
                {trend_html}
            </div>
            """
        )
    st.markdown(f'<div class="pq-kpi-grid">{"".join(tile_html)}</div>', unsafe_allow_html=True)


def _render_tour() -> None:
    """3-step explainer surfaced on the Overview tab."""
    st.markdown(
        """
        <div class="pq-tour">
            <div style="display:flex;align-items:baseline;justify-content:space-between;gap:.6rem;flex-wrap:wrap;">
                <div>
                    <span class="pq-page-eyebrow">How it works</span>
                    <h3 style="margin:.2rem 0 0;color:#fff;font-weight:800;font-size:1.05rem;">Find your edge in three steps</h3>
                </div>
                <span class="pq-chip muted">No account required to explore</span>
            </div>
            <div class="pq-tour-grid">
                <div class="pq-tour-card">
                    <span class="pq-tour-step">Step 1 · Scan</span>
                    <h4>Discover live edges</h4>
                    <p>Jump to <strong>Value Plays</strong> for elite EV anomalies, or browse <strong>Markets</strong>
                    to search every live Polymarket and Kalshi contract.</p>
                </div>
                <div class="pq-tour-card">
                    <span class="pq-tour-step">Step 2 · Validate</span>
                    <h4>Run the numbers</h4>
                    <p>Drop your win-rate into the <strong>Bet Calculator</strong> to see EV, Kelly sizing and a
                    pass/play verdict in one click.</p>
                </div>
                <div class="pq-tour-card">
                    <span class="pq-tour-step">Step 3 · Lock</span>
                    <h4>Pair for risk-free profit</h4>
                    <p>Use the <strong>Arbs</strong> tab to cross-book the exact same event on both exchanges and
                    lock guaranteed profit when prices diverge.</p>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _category_breakdown(catalog: pd.DataFrame) -> pd.DataFrame:
    """Counts by category + source for the overview chart."""
    if catalog.empty:
        return pd.DataFrame()
    pivot = (
        catalog.groupby(["Category", "Source"]).size().unstack(fill_value=0).sort_values
        if False else
        catalog.pivot_table(index="Category", columns="Source", aggfunc="size", fill_value=0)
    )
    pivot = pivot.reindex(
        index=pivot.sum(axis=1).sort_values(ascending=False).index
    )
    return pivot


def _arb_opportunity_scan(
    poly_df: pd.DataFrame,
    kalshi_df: pd.DataFrame,
    top_n: int = 5,
    min_score: float = 0.28,
) -> pd.DataFrame:
    """Cheap pairwise arb scan for the overview tab — title-match heuristic."""
    if poly_df.empty or kalshi_df.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for _, p in poly_df.iterrows():
        p_yes = _coerce_float(p.get("Yes Price"))
        p_no = _coerce_float(p.get("No Price"))
        if p_yes is None or p_no is None or p_yes <= 0 or p_no <= 0:
            continue
        ranked = _rank_kalshi_for_poly(str(p.get("Question", "")), kalshi_df, top_n=1)
        if not ranked:
            continue
        score, ticker, ktitle = ranked[0]
        if score < min_score:
            continue
        krow = kalshi_df.loc[kalshi_df["ticker"] == ticker]
        if krow.empty:
            continue
        krow = krow.iloc[0]
        k_yes = _coerce_float(krow.get("Kalshi YES Cost"))
        k_no = _coerce_float(krow.get("Kalshi NO Cost"))
        if k_yes is None or k_no is None:
            continue
        cost_a = p_yes + k_no
        cost_b = p_no + k_yes
        best_cost = min(cost_a, cost_b)
        leg_a = ("Poly YES + Kalshi NO", cost_a)
        leg_b = ("Poly NO + Kalshi YES", cost_b)
        best_leg, _ = min([leg_a, leg_b], key=lambda x: x[1])
        edge_pct = (1.0 - best_cost) * 100.0
        rows.append({
            "Polymarket market": str(p.get("Question", ""))[:80],
            "Kalshi match": str(ktitle)[:80],
            "Strategy": best_leg,
            "Combined cost ¢": round(best_cost * 100.0, 1),
            "Edge ¢": round((1.0 - best_cost) * 100.0, 1),
            "Edge %": round(edge_pct, 2),
            "Match score": round(score * 100.0, 0),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("Edge %", ascending=False).head(top_n).reset_index(drop=True)
    return df


def render_overview_tab() -> None:
    """Command-center landing page: KPIs, charts, tour, and quick actions."""
    _page_header(
        "Command center",
        "Welcome back",
        "A single view of every live edge across Polymarket and Kalshi. Scan markets, validate bets, "
        "and lock cross-book profits — all in one terminal.",
    )

    # ---- Data load (best-effort, never crash the overview) ----
    poly_df = pd.DataFrame()
    kalshi_df = pd.DataFrame()
    catalog = pd.DataFrame()
    value_plays = pd.DataFrame()

    try:
        poly_df = fetch_polymarket_markets()
    except Exception:
        pass
    try:
        kalshi_main = fetch_kalshi_markets()
        kalshi_props = fetch_kalshi_player_props()
        kalshi_df = pd.concat([kalshi_main, kalshi_props], ignore_index=True).drop_duplicates(
            subset=["ticker"]
        )
        kalshi_df = _filter_kalshi_tradeable(kalshi_df)
    except Exception:
        pass
    try:
        catalog = build_explore_catalog()
    except Exception:
        pass
    try:
        value_plays = _filter_value_plays(poly_df) if not poly_df.empty else pd.DataFrame()
    except Exception:
        pass

    # ---- KPI tiles ----
    markets_total = int(len(catalog)) if not catalog.empty else int(len(poly_df) + len(kalshi_df))
    poly_count = int(len(poly_df)) if not poly_df.empty else 0
    kalshi_count = int(len(kalshi_df)) if not kalshi_df.empty else 0
    elite_count = int(len(value_plays)) if not value_plays.empty else 0
    sharpest_edge = (
        float(value_plays["Net EV Edge %"].max()) if not value_plays.empty else 0.0
    )
    total_vol = float(poly_df["Volume"].sum()) if "Volume" in poly_df.columns else 0.0

    def _short_money(amount: float) -> str:
        if amount >= 1_000_000:
            return f"${amount / 1_000_000:.1f}M"
        if amount >= 1_000:
            return f"${amount / 1_000:.0f}K"
        return f"${amount:,.0f}"

    _render_kpi_grid([
        {"label": "Tracked markets", "value": f"{markets_total:,}",
         "trend": f"{poly_count:,} Polymarket · {kalshi_count:,} Kalshi", "tone": "accent"},
        {"label": "Elite value plays",
         "value": f"{elite_count}",
         "trend": (f"Top edge +{sharpest_edge:.1f}% net EV" if elite_count else "No anomalies right now"),
         "trend_cls": "pos" if elite_count else "",
         "tone": "success"},
        {"label": "Live 24h volume",
         "value": _short_money(total_vol),
         "trend": "Polymarket reported volume", "tone": "violet"},
        {"label": "Books connected",
         "value": "2 / 2" if (poly_count and kalshi_count) else ("1 / 2" if (poly_count or kalshi_count) else "0 / 2"),
         "trend": "Polymarket · Kalshi", "tone": "warn"},
    ])

    # ---- 3-step tour ----
    _render_tour()

    # ---- Quick actions row ----
    st.markdown(
        '<div class="pq-section-heading"><h3>Quick actions</h3>'
        '<span class="hint">Jump straight into the workflow you need.</span></div>',
        unsafe_allow_html=True,
    )
    qa1, qa2, qa3, qa4 = st.columns(4)
    with qa1:
        if st.button("🔥 See Value Plays", use_container_width=True, type="primary",
                     key="overview_btn_plays"):
            st.session_state["_overview_hint"] = "Open the **Value Plays** tab above to see filtered edges."
    with qa2:
        if st.button("🔍 Browse Markets", use_container_width=True, key="overview_btn_explore"):
            st.session_state["_overview_hint"] = "Open the **Markets** tab to search every live contract."
    with qa3:
        if st.button("🧮 Run Calculator", use_container_width=True, key="overview_btn_audit"):
            st.session_state["_overview_hint"] = "Open the **Bet Calculator** tab to score any line."
    with qa4:
        if st.button("💰 Hunt Arbs", use_container_width=True, key="overview_btn_arb"):
            st.session_state["_overview_hint"] = "Open the **Arbs** tab to lock a cross-book pair."

    if st.session_state.get("_overview_hint"):
        st.info(st.session_state["_overview_hint"])

    # ---- Top edges preview ----
    st.markdown(
        '<div class="pq-section-heading"><h3>🔥 Today\'s sharpest edges</h3>'
        '<span class="hint">Net EV after platform fees · top 5 of the elite tier.</span></div>',
        unsafe_allow_html=True,
    )

    if value_plays.empty:
        st.markdown(
            """
            <div class="pq-card" style="text-align:center;padding:1.6rem 1.1rem;">
                <p class="pq-event-name" style="margin-bottom:0.5rem;">No elite anomalies on the slate</p>
                <p style="color:var(--pq-text-muted);font-size:0.9rem;margin:0;line-height:1.5;">
                    Strict gates: win prob &gt;75% and net EV ≥5%. Markets refresh every minute — sit tight or
                    drop into <strong>Markets</strong> to scan the wider universe.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        odds_fmt = get_odds_format()
        preview_rows: list[dict[str, Any]] = []
        for i, (_, row) in enumerate(value_plays.head(5).iterrows(), start=1):
            preview_rows.append({
                "#": i,
                "Market": str(row["Question"])[:90],
                "NO line": format_odds_display(float(row["No Price"]), odds_fmt),
                "Implied %": round(float(row["No Price"]) * 100.0, 1),
                "Model %": round(float(row["Model Win %"]), 1),
                "Edge %": round(float(row["Net EV Edge %"]), 2),
            })
        st.dataframe(
            pd.DataFrame(preview_rows),
            use_container_width=True,
            hide_index=True,
            height=48 * len(preview_rows) + 60,
            column_config={
                "#": st.column_config.NumberColumn("#", width="small"),
                "Market": st.column_config.TextColumn("Market", width="large"),
                "NO line": st.column_config.TextColumn("NO line", width="small"),
                "Implied %": st.column_config.ProgressColumn(
                    "Market implied", min_value=0, max_value=100, format="%.1f%%"
                ),
                "Model %": st.column_config.ProgressColumn(
                    "Model true", min_value=0, max_value=100, format="%.1f%%"
                ),
                "Edge %": st.column_config.NumberColumn("Net EV edge", format="+%.2f%%"),
            },
        )

    # ---- Market mix charts ----
    if not catalog.empty:
        st.markdown(
            '<div class="pq-section-heading"><h3>📊 Market universe</h3>'
            '<span class="hint">Every live contract, broken down by category and book.</span></div>',
            unsafe_allow_html=True,
        )
        try:
            pivot = _category_breakdown(catalog)
            if not pivot.empty:
                ch_l, ch_r = st.columns([1.45, 1])
                with ch_l:
                    st.bar_chart(pivot, use_container_width=True, height=260)
                    st.caption("Markets per category, split by exchange.")
                with ch_r:
                    src = catalog.groupby("Source").size().rename("Markets").to_frame()
                    st.bar_chart(src, use_container_width=True, height=260, color="#3b82f6")
                    st.caption("Total live markets per exchange.")
        except Exception:
            pass

    # ---- Implied-probability distribution + arb leaderboard ----
    if not poly_df.empty:
        st.markdown(
            '<div class="pq-section-heading"><h3>📈 Pricing & arb radar</h3>'
            '<span class="hint">Where the market is currently priced, and the closest cross-book pairs.</span></div>',
            unsafe_allow_html=True,
        )
        dist_l, dist_r = st.columns([1, 1.3])
        with dist_l:
            try:
                no_prices = (
                    poly_df["No Price"].dropna().astype(float).clip(0.01, 0.99) * 100.0
                )
                if not no_prices.empty:
                    bins = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
                    labels = ["0-10", "10-20", "20-30", "30-40", "40-50",
                              "50-60", "60-70", "70-80", "80-90", "90-100"]
                    binned = pd.cut(no_prices, bins=bins, labels=labels, include_lowest=True)
                    counts = binned.value_counts().sort_index().rename("Markets").to_frame()
                    st.bar_chart(counts, use_container_width=True, height=260, color="#8b5cf6")
                    st.caption("Polymarket NO-price distribution (% bins).")
            except Exception:
                pass
        with dist_r:
            try:
                arbs = _arb_opportunity_scan(poly_df, kalshi_df, top_n=5)
                if arbs.empty:
                    st.markdown(
                        '<div class="pq-card" style="text-align:center;padding:1.4rem 1rem;">'
                        '<p style="margin:0;font-weight:700;color:#fff;">No cross-book pairs are arb-priced right now.</p>'
                        '<p style="margin:.3rem 0 0;color:var(--pq-text-muted);font-size:.85rem;line-height:1.5;">'
                        "Drop into the <strong>Arbs</strong> tab to pair markets manually and watch for movement."
                        "</p></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.dataframe(
                        arbs,
                        use_container_width=True,
                        hide_index=True,
                        height=48 * len(arbs) + 60,
                        column_config={
                            "Polymarket market": st.column_config.TextColumn(width="medium"),
                            "Kalshi match": st.column_config.TextColumn(width="medium"),
                            "Strategy": st.column_config.TextColumn(width="small"),
                            "Combined cost ¢": st.column_config.NumberColumn(format="%.1f¢"),
                            "Edge ¢": st.column_config.NumberColumn(format="%.1f¢"),
                            "Edge %": st.column_config.ProgressColumn(
                                "Edge %", min_value=0, max_value=10, format="%.2f%%"
                            ),
                            "Match score": st.column_config.NumberColumn(format="%.0f%%"),
                        },
                    )
                    st.caption(
                        "Title-matched arb candidates — open **💰 Arbs** to ticket sizes and exact payouts."
                    )
            except Exception:
                pass

    # ---- Glossary / help ----
    with st.expander("📚 Cheat sheet — terms used in this app", expanded=False):
        st.markdown(
            """
**Net EV edge** — Expected value of a $1 stake after platform fees, expressed as a %. Anything above 5% clears the elite tier.

**Implied probability** — What the market price says the event's chance is (e.g. a 30¢ NO ⇒ 30% implied).

**True / model probability** — Our reference probability used to find pricing mistakes.

**Kelly allocation** — How much of your bankroll to risk given your edge. The Calculator shows it for every input.

**Arb / risk-free pair** — Buying both sides of the same event across exchanges at a combined cost under $1 — every outcome pays out.

**Combined cost** — `Poly leg ¢ + Kalshi leg ¢`. Lower than 100¢ ⇒ locked profit.
            """
        )


# --------------------------------------------------------------------------- #
# App shell
# --------------------------------------------------------------------------- #

st.set_page_config(
    page_title=f"POLY-QUANT · {APP_BUILD}",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_inject_global_css()
_init_session()


def _render_app_header() -> None:
    """Polished sticky-feeling header with brand, build chip and live status."""
    st.markdown(
        f"""
        <div class="pq-hero-shell">
            <div class="pq-hero-row">
                <div class="pq-hero-brand">
                    <div class="pq-hero-logo">PQ</div>
                    <div>
                        <p class="pq-hero-title">POLY-QUANT</p>
                        <p class="pq-hero-sub">Cross-book betting intelligence · Polymarket × Kalshi</p>
                    </div>
                </div>
                <div class="pq-hero-meta">
                    <span class="pq-chip live">Live markets</span>
                    <span class="pq-chip build">Build {html.escape(APP_BUILD)}</span>
                    <span class="pq-chip muted">commit&nbsp;<code>{html.escape(GIT_SHA)}</code></span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


_render_app_header()

tool_l, tool_r = st.columns([3, 1])
with tool_l:
    render_global_search_bar()
with tool_r:
    render_odds_format_toggle()


def main() -> None:
    (
        tab_home,
        tab_plays,
        tab_explore,
        tab_audit,
        tab_hype,
        tab_arb,
        tab_ledger,
    ) = st.tabs(
        [
            "🏠 Overview",
            "🔥 Value Plays",
            "🔍 Markets",
            "🧮 Bet Calculator",
            "📣 Sentiment",
            "💰 Arbs",
            "📒 Ledger",
        ]
    )

    with tab_home:
        render_overview_tab()

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
