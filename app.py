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
APP_BUILD = "4.0.0-command-center-overhaul"
GIT_SHA = "b556168+"

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
            .pq-arb-detail {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 0.8rem;
                margin-top: 0.7rem;
            }
            .pq-arb-detail-title {
                margin: 0 0 0.5rem;
                color: #f0f2f5;
                font-size: 0.78rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.06em;
            }
            .pq-arb-ticket-row {
                display: grid;
                grid-template-columns: 1.25fr 0.75fr 0.8fr 0.9fr;
                gap: 0.45rem;
                align-items: center;
                padding: 0.5rem 0;
                border-top: 1px solid #21262d;
                font-size: 0.78rem;
                color: #c9d1d9;
            }
            .pq-arb-ticket-row.header {
                border-top: 0;
                padding-top: 0;
                color: #8b949e;
                font-size: 0.66rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .pq-arb-ticket-row strong {
                color: #f0f2f5;
                font-weight: 800;
            }
            .pq-arb-ticket-row .cash {
                color: #58a6ff;
                font-weight: 800;
                text-align: right;
            }
            .pq-arb-explain {
                margin: 0.65rem 0 0;
                color: #c9d1d9;
                font-size: 0.82rem;
                line-height: 1.48;
            }
            .pq-arb-explain strong { color: #f0f2f5; }
            .pq-arb-warning {
                background: rgba(248,81,73,0.12);
                border: 1px solid rgba(248,81,73,0.45);
                border-radius: 10px;
                color: #ffb3ad;
                font-size: 0.8rem;
                line-height: 1.45;
                margin-top: 0.7rem;
                padding: 0.7rem 0.75rem;
            }
            .pq-arb-spotlight {
                background: linear-gradient(180deg, rgba(88,166,255,0.18), rgba(13,17,23,0.96));
                border: 2px solid #58a6ff;
                border-radius: 16px;
                box-shadow: 0 0 26px rgba(88,166,255,0.18);
                margin: 0.75rem 0 1rem;
                padding: 1rem;
            }
            .pq-arb-spotlight.live {
                background: linear-gradient(180deg, rgba(63,185,80,0.22), rgba(13,17,23,0.96));
                border-color: #3fb950;
                box-shadow: 0 0 28px rgba(63,185,80,0.22);
            }
            .pq-arb-spotlight.dead {
                background: linear-gradient(180deg, rgba(248,81,73,0.16), rgba(13,17,23,0.96));
                border-color: #f85149;
                box-shadow: 0 0 24px rgba(248,81,73,0.15);
            }
            .pq-arb-spotlight-kicker {
                color: #8b949e;
                font-size: 0.68rem;
                font-weight: 900;
                letter-spacing: 0.08em;
                margin: 0 0 0.25rem;
                text-transform: uppercase;
            }
            .pq-arb-spotlight-title {
                color: #f0f2f5;
                font-size: 1.1rem;
                font-weight: 900;
                letter-spacing: -0.02em;
                line-height: 1.2;
                margin: 0 0 0.7rem;
            }
            .pq-arb-action-list {
                display: grid;
                gap: 0.5rem;
                margin: 0.7rem 0;
            }
            .pq-arb-action {
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 0.75rem;
            }
            .pq-arb-action .step {
                color: #8b949e;
                display: block;
                font-size: 0.66rem;
                font-weight: 900;
                letter-spacing: 0.06em;
                text-transform: uppercase;
            }
            .pq-arb-action .take {
                color: #f0f2f5;
                display: block;
                font-size: 0.94rem;
                font-weight: 900;
                margin-top: 0.15rem;
            }
            .pq-arb-action .meta {
                color: #58a6ff;
                display: block;
                font-size: 0.78rem;
                font-weight: 700;
                margin-top: 0.2rem;
            }
            .pq-arb-spotlight-note {
                color: #c9d1d9;
                font-size: 0.82rem;
                line-height: 1.5;
                margin: 0.65rem 0 0;
            }
            .pq-arb-spotlight-note strong { color: #f0f2f5; }
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


def _inject_ui_overhaul_css() -> None:
    """Final-pass shell styles that modernize hierarchy and workflow readability."""
    st.markdown(
        """
        <style>
            :root {
                --pq-bg-0: #070b14;
                --pq-bg-1: #0e1526;
                --pq-surface: #111a2d;
                --pq-surface-soft: rgba(22, 33, 58, 0.76);
                --pq-border: rgba(111, 138, 187, 0.35);
                --pq-text: #ebf1ff;
                --pq-muted: #94a3c4;
                --pq-accent: #4f9cff;
                --pq-success: #31c48d;
                --pq-warn: #f59e0b;
            }

            .stApp {
                background:
                    radial-gradient(1200px 520px at 12% -5%, rgba(79, 156, 255, 0.20), transparent 54%),
                    radial-gradient(900px 500px at 100% 0%, rgba(49, 196, 141, 0.15), transparent 58%),
                    linear-gradient(180deg, var(--pq-bg-0) 0%, var(--pq-bg-1) 55%, #0b1220 100%) !important;
                color: var(--pq-text) !important;
            }

            .block-container {
                max-width: 1200px !important;
                padding-top: 0.85rem !important;
                padding-bottom: 2.4rem !important;
            }

            [data-testid="stMetricContainer"] {
                background: var(--pq-surface-soft) !important;
                border: 1px solid var(--pq-border) !important;
                border-radius: 14px !important;
                padding: 0.85rem 0.95rem !important;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
            }

            [data-testid="stMetricLabel"] {
                color: var(--pq-muted) !important;
                font-size: 0.78rem !important;
                font-weight: 600 !important;
                letter-spacing: 0.01em;
            }

            [data-testid="stMetricValue"] {
                color: var(--pq-text) !important;
                font-size: 1.24rem !important;
                font-weight: 780 !important;
            }

            .stButton > button {
                border-radius: 10px !important;
                border: 1px solid rgba(99, 140, 210, 0.45) !important;
                background: linear-gradient(180deg, rgba(27, 45, 80, 0.92), rgba(21, 34, 60, 0.92)) !important;
                color: #ecf3ff !important;
                font-weight: 650 !important;
            }
            .stButton > button:hover {
                border-color: rgba(126, 167, 236, 0.65) !important;
                box-shadow: 0 0 0 1px rgba(79, 156, 255, 0.3) inset;
            }

            .pq-shell-hero {
                background: linear-gradient(
                    125deg,
                    rgba(22, 37, 66, 0.95) 0%,
                    rgba(16, 27, 49, 0.95) 52%,
                    rgba(22, 52, 46, 0.85) 100%
                );
                border: 1px solid rgba(124, 162, 224, 0.35);
                border-radius: 18px;
                padding: 1rem 1.15rem 0.95rem;
                margin-bottom: 0.8rem;
                box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28);
            }
            .pq-shell-kicker {
                margin: 0;
                color: #91b5f8;
                font-size: 0.74rem;
                font-weight: 760;
                letter-spacing: 0.06em;
                text-transform: uppercase;
            }
            .pq-shell-title {
                margin: 0.25rem 0 0.2rem;
                color: #f8fbff;
                font-size: 1.55rem;
                font-weight: 830;
                letter-spacing: -0.02em;
            }
            .pq-shell-subtitle {
                margin: 0;
                color: #b5c5e8;
                font-size: 0.88rem;
                line-height: 1.45;
            }
            .pq-shell-pills {
                margin-top: 0.72rem;
                display: flex;
                flex-wrap: wrap;
                gap: 0.45rem;
            }
            .pq-shell-pill {
                border: 1px solid rgba(116, 154, 222, 0.38);
                background: rgba(18, 31, 56, 0.72);
                color: #d4e4ff;
                border-radius: 999px;
                padding: 0.2rem 0.58rem;
                font-size: 0.72rem;
                font-weight: 650;
            }

            .pq-kpi-note {
                margin: 0.35rem 0 0.6rem;
                font-size: 0.8rem;
                color: var(--pq-muted);
            }

            .pq-highlight-card {
                background: linear-gradient(135deg, rgba(79, 156, 255, 0.17), rgba(49, 196, 141, 0.1));
                border: 1px solid rgba(113, 170, 246, 0.35);
                border-radius: 14px;
                padding: 0.9rem 1rem;
                margin: 0.3rem 0 0.85rem;
            }
            .pq-highlight-title {
                margin: 0 0 0.2rem;
                font-size: 0.74rem;
                font-weight: 750;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: #8fbbff;
            }
            .pq-highlight-body {
                margin: 0;
                font-size: 0.92rem;
                line-height: 1.5;
                color: #e7f0ff;
            }
            .pq-highlight-body strong {
                color: #ffffff;
            }

            .pq-workflow-card {
                background: rgba(15, 24, 43, 0.86);
                border: 1px solid rgba(101, 134, 193, 0.32);
                border-radius: 14px;
                padding: 0.85rem 0.9rem;
                min-height: 155px;
            }
            .pq-workflow-step {
                margin: 0;
                font-size: 0.74rem;
                font-weight: 750;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: #9cc0ff;
            }
            .pq-workflow-title {
                margin: 0.3rem 0 0.35rem;
                font-size: 0.98rem;
                font-weight: 730;
                color: #f2f7ff;
            }
            .pq-workflow-copy {
                margin: 0;
                color: #b8c8e9;
                line-height: 1.45;
                font-size: 0.82rem;
            }

            .pq-segment-title {
                margin: 0.1rem 0 0.2rem;
                font-size: 1.12rem;
                font-weight: 790;
                color: #eef4ff;
                letter-spacing: -0.01em;
            }

            .pq-health-ok { color: var(--pq-success); font-weight: 700; }
            .pq-health-warn { color: var(--pq-warn); font-weight: 700; }
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
    st.markdown("### 🔥 Value Plays")
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

    avg_edge = float(df["Net EV Edge %"].mean())
    avg_model = float(df["Model Win %"].mean())
    total_volume = float(df["Volume"].sum())
    k1, k2, k3 = st.columns(3)
    k1.metric("Qualified plays", f"{len(df)}")
    k2.metric("Average model edge", f"+{avg_edge:.1f}%")
    k3.metric("Combined liquidity", _compact_dollar(total_volume))
    st.caption(f"Average model win probability across qualified plays: {avg_model:.1f}%")

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
    st.markdown("### 🔎 Discover")
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

    source_mix = filtered["Source"].value_counts() if "Source" in filtered.columns else pd.Series(dtype=float)
    poly_count = int(source_mix.get("Polymarket", 0))
    kalshi_count = int(source_mix.get("Kalshi", 0))
    c1, c2, c3 = st.columns(3)
    c1.metric("Filtered markets", f"{len(filtered)}")
    c2.metric("Polymarket coverage", f"{poly_count}")
    c3.metric("Kalshi coverage", f"{kalshi_count}")

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
            st.session_state.explore_action_hint = "Switch to the **✅ Bet Check** tab to run the math."
            st.rerun()
    with qa2:
        if st.button("💰 Cross-Book Arb", use_container_width=True):
            st.session_state.explore_action_hint = (
                "Switch to the **🧩 Arbs** tab — your pick is pre-loaded."
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
    st.markdown("### 🧩 Cross-Book Arbs")
    st.caption(
        "Select one market per exchange. The engine computes exact sizing, outlay, payout, "
        "guaranteed profit, and tells you when no lock currently exists."
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
    best_total_cost = min(float(item["total_cost"]) for item in strategies) * 100.0
    best_roi = max(float(item["roi"]) for item in strategies)
    locked_count = sum(1 for item in strategies if bool(item["is_arb"]))
    a1, a2, a3 = st.columns(3)
    a1.metric("Best combined cost", f"{best_total_cost:.1f}¢")
    a2.metric("Best ROI", f"{best_roi:+.2f}%")
    a3.metric("Locked strategies", f"{locked_count}/{len(strategies)}")

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


def _safe_fetch_df(fetcher: Any) -> tuple[pd.DataFrame, Optional[str]]:
    """Fetch a DataFrame and return a readable error source if it fails."""
    try:
        data = fetcher()
    except Exception:
        return pd.DataFrame(), getattr(fetcher, "__name__", "unknown")
    if isinstance(data, pd.DataFrame):
        return data, None
    return pd.DataFrame(), getattr(fetcher, "__name__", "unknown")


def _safe_pct(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return (part / total) * 100.0


def _compact_dollar(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def _estimate_live_arb_count(poly_df: pd.DataFrame, kalshi_df: pd.DataFrame) -> tuple[int, int]:
    """
    Fast heuristic: sample Polymarket rows, match best Kalshi title, count pairs where
    Poly YES + Kalshi NO or Poly NO + Kalshi YES is below $1.00.
    """
    if poly_df.empty or kalshi_df.empty:
        return 0, 0

    required_poly = {"Question", "Yes Price", "No Price"}
    required_k = {"ticker", "Kalshi YES Cost", "Kalshi NO Cost", "Title"}
    if not required_poly.issubset(poly_df.columns) or not required_k.issubset(kalshi_df.columns):
        return 0, 0

    poly_priced = poly_df.dropna(subset=["Question", "Yes Price", "No Price"]).copy()
    kalshi_priced = _filter_kalshi_tradeable(kalshi_df)
    if poly_priced.empty or kalshi_priced.empty:
        return 0, 0

    sample = poly_priced.head(40)
    sampled = len(sample)
    if sampled == 0:
        return 0, 0

    kalshi_lookup = kalshi_priced.set_index("ticker", drop=False)
    live_count = 0
    for _, row in sample.iterrows():
        suggestions = _rank_kalshi_for_poly(str(row["Question"]), kalshi_priced, top_n=1)
        if not suggestions:
            continue
        _, ticker, _ = suggestions[0]
        if ticker not in kalshi_lookup.index:
            continue
        k_row = kalshi_lookup.loc[ticker]
        if isinstance(k_row, pd.DataFrame):
            k_row = k_row.iloc[0]
        cost_a = float(row["Yes Price"]) + float(k_row["Kalshi NO Cost"])
        cost_b = float(row["No Price"]) + float(k_row["Kalshi YES Cost"])
        if cost_a < 1.0 or cost_b < 1.0:
            live_count += 1
    return live_count, sampled


@st.cache_data(ttl=45, show_spinner=False)
def build_dashboard_snapshot() -> dict[str, Any]:
    """Aggregate all key UI KPIs so the shell can stay data-first and readable."""
    fetch_errors: list[str] = []

    poly_df, poly_err = _safe_fetch_df(fetch_polymarket_markets)
    if poly_err:
        fetch_errors.append("Polymarket")

    kalshi_main, kalshi_err = _safe_fetch_df(fetch_kalshi_markets)
    if kalshi_err:
        fetch_errors.append("Kalshi")

    kalshi_props, props_err = _safe_fetch_df(fetch_kalshi_player_props)
    if props_err:
        fetch_errors.append("Kalshi props")

    if kalshi_main.empty and kalshi_props.empty:
        kalshi_all = pd.DataFrame()
    else:
        kalshi_all = pd.concat([kalshi_main, kalshi_props], ignore_index=True)
        if "ticker" in kalshi_all.columns:
            kalshi_all = kalshi_all.drop_duplicates(subset=["ticker"])

    catalog, catalog_err = _safe_fetch_df(build_explore_catalog)
    if catalog_err:
        fetch_errors.append("Explore catalog")

    value_plays = _filter_value_plays(poly_df) if not poly_df.empty else pd.DataFrame()
    avg_edge = float(value_plays["Net EV Edge %"].mean()) if not value_plays.empty else 0.0
    top_value_title = str(value_plays.iloc[0]["Question"]) if not value_plays.empty else ""
    top_value_edge = float(value_plays.iloc[0]["Net EV Edge %"]) if not value_plays.empty else 0.0

    live_arb_count, arb_sample_size = _estimate_live_arb_count(poly_df, kalshi_all)

    creds = _ledger_credentials()
    connected_books = int(creds["kalshi"]) + int(creds["polymarket"])
    ledger = _empty_ledger()
    if connected_books:
        try:
            ledger = fetch_unified_ledger()
        except Exception:
            fetch_errors.append("Ledger")
            ledger = _empty_ledger()

    settled = ledger[ledger["Status"].isin(["WON", "LOST"])].copy() if not ledger.empty else pd.DataFrame()
    settled_count = int(len(settled))
    wins = int((settled["Net Return $"] > 0).sum()) if not settled.empty else 0
    win_rate = _safe_pct(wins, settled_count)
    settled_pnl = float(settled["Net Return $"].sum()) if not settled.empty else 0.0
    open_risk = float(ledger.loc[ledger["Status"] == "OPEN", "Stake $"].sum()) if not ledger.empty else 0.0

    category_mix: list[tuple[str, int]] = []
    if not catalog.empty and "Category" in catalog.columns:
        for cat, count in catalog["Category"].value_counts().head(4).items():
            category_mix.append((str(cat), int(count)))

    poly_volume = float(poly_df["Volume"].fillna(0).sum()) if "Volume" in poly_df.columns else 0.0

    return {
        "fetch_errors": sorted(set(fetch_errors)),
        "poly_market_count": int(len(poly_df)),
        "kalshi_market_count": int(len(kalshi_all)),
        "catalog_total": int(len(catalog)),
        "value_play_count": int(len(value_plays)),
        "avg_value_edge": avg_edge,
        "top_value_title": top_value_title,
        "top_value_edge": top_value_edge,
        "live_arb_count": int(live_arb_count),
        "arb_sample_size": int(arb_sample_size),
        "poly_volume": poly_volume,
        "connected_books": connected_books,
        "settled_count": settled_count,
        "win_rate": win_rate,
        "settled_pnl": settled_pnl,
        "open_risk": open_risk,
        "category_mix": category_mix,
        "refreshed_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }


def _render_shell_header(snapshot: dict[str, Any]) -> None:
    healthy = not snapshot["fetch_errors"]
    health_cls = "pq-health-ok" if healthy else "pq-health-warn"
    health_text = "Data feeds healthy" if healthy else "Partial data feed degradation"

    pills = [
        f"Build {APP_BUILD}",
        f"{snapshot['catalog_total']:,} markets indexed",
        f"{snapshot['value_play_count']} elite value plays",
        f"{snapshot['live_arb_count']} potential arbs",
        f"Refreshed {snapshot['refreshed_at']}",
    ]
    pill_html = "".join(
        f'<span class="pq-shell-pill">{html.escape(item)}</span>'
        for item in pills
    )

    st.markdown(
        f"""
        <div class="pq-shell-hero">
            <p class="pq-shell-kicker">Prediction Market Intelligence</p>
            <p class="pq-shell-title">POLY-QUANT Command Center</p>
            <p class="pq-shell-subtitle">
                Discover edges, validate bets, execute arbs, and track performance in one guided workflow.
                <span class="{health_cls}">{health_text}</span>
            </p>
            <div class="pq-shell-pills">{pill_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_command_center() -> None:
    st.markdown('<p class="pq-segment-title">Command Center</p>', unsafe_allow_html=True)
    snapshot = build_dashboard_snapshot()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Markets Indexed",
        f"{snapshot['catalog_total']:,}",
        delta=f"{snapshot['poly_market_count']:,} Poly · {snapshot['kalshi_market_count']:,} Kalshi",
    )
    m2.metric(
        "Elite Value Plays",
        f"{snapshot['value_play_count']}",
        delta=f"Avg edge {snapshot['avg_value_edge']:+.1f}%",
    )
    m3.metric(
        "Estimated Live Arbs",
        f"{snapshot['live_arb_count']}",
        delta=f"from {snapshot['arb_sample_size']} sampled cross-book pairs",
    )
    m4.metric(
        "Tracked Daily Volume",
        _compact_dollar(snapshot["poly_volume"]),
        delta="Polymarket notional",
    )
    st.markdown(
        '<p class="pq-kpi-note">Tip: use global search to narrow markets before opening each workflow tab.</p>',
        unsafe_allow_html=True,
    )

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Connected Books", f"{snapshot['connected_books']}/2")
    k2.metric("Settled Bets", f"{snapshot['settled_count']}")
    k3.metric("Win Rate", f"{snapshot['win_rate']:.1f}%")
    k4.metric("Open Risk", _compact_dollar(snapshot["open_risk"]))

    if snapshot["top_value_title"]:
        st.markdown(
            f"""
            <div class="pq-highlight-card">
                <p class="pq-highlight-title">Top opportunity right now</p>
                <p class="pq-highlight-body">
                    <strong>{html.escape(snapshot["top_value_title"])}</strong><br>
                    Net edge <strong>+{snapshot["top_value_edge"]:.2f}%</strong>.
                    Start in <strong>🔥 Value Plays</strong>, then run the same line through
                    <strong>✅ Bet Check</strong> before sizing.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info(
            "No elite value anomalies detected right now. Start in Discover to monitor line moves and wait for better pricing."
        )

    st.markdown("#### Guided workflow")
    wf1, wf2, wf3 = st.columns(3)
    with wf1:
        st.markdown(
            """
            <div class="pq-workflow-card">
                <p class="pq-workflow-step">Step 1</p>
                <p class="pq-workflow-title">Discover markets</p>
                <p class="pq-workflow-copy">
                    Use <strong>🔎 Discover</strong> to filter by category/source and quickly shortlist opportunities.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with wf2:
        st.markdown(
            """
            <div class="pq-workflow-card">
                <p class="pq-workflow-step">Step 2</p>
                <p class="pq-workflow-title">Validate expected value</p>
                <p class="pq-workflow-copy">
                    Open <strong>✅ Bet Check</strong> to stress-test pricing, EV, and bankroll sizing before entry.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with wf3:
        st.markdown(
            """
            <div class="pq-workflow-card">
                <p class="pq-workflow-step">Step 3</p>
                <p class="pq-workflow-title">Execute + track</p>
                <p class="pq-workflow-copy">
                    Use <strong>🧩 Arbs</strong> for paired execution, then monitor outcomes in <strong>📒 Ledger</strong>.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if snapshot["category_mix"]:
        mix = " · ".join(f"{name}: {count}" for name, count in snapshot["category_mix"])
        st.caption(f"Market mix: {mix}")

    if snapshot["fetch_errors"]:
        errs = ", ".join(snapshot["fetch_errors"])
        st.warning(
            f"Some feeds are temporarily unavailable ({errs}). The app remains usable with available sources."
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

st.markdown(
    """
    <style>
        header {visibility: hidden;}
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)

_inject_global_css()
_inject_ui_overhaul_css()
_init_session()
shell_snapshot = build_dashboard_snapshot()
_render_shell_header(shell_snapshot)

tool_l, tool_r = st.columns([3, 1])
with tool_l:
    render_global_search_bar()
with tool_r:
    render_odds_format_toggle()


def main() -> None:
    (
        tab_home,
        tab_discover,
        tab_plays,
        tab_audit,
        tab_hype,
        tab_arb,
        tab_ledger,
    ) = st.tabs(
        [
            "🏠 Dashboard",
            "🔎 Discover",
            "🔥 Value Plays",
            "✅ Bet Check",
            "🧠 Sentiment",
            "🧩 Arbs",
            "📒 Ledger",
        ]
    )

    with tab_home:
        render_command_center()

    with tab_discover:
        render_explore_hub()

    with tab_plays:
        render_top_value_plays()

    with tab_audit:
        render_audit_my_bet()

    with tab_hype:
        render_hype_vs_reality()

    with tab_arb:
        render_risk_free_arbs()

    with tab_ledger:
        render_ledger()


main()
