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
APP_BUILD = "3.2.0-arb-action-panel"
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
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap');

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            :root {
                --bg-base: #060a14;
                --bg-surface: #0b1022;
                --bg-elevated: #101525;
                --bg-input: #090d1c;
                --border-subtle: #181e31;
                --border-default: #222847;
                --border-emphasis: #2e3a5e;
                --primary: #5b7af5;
                --primary-dim: rgba(91,122,245,0.1);
                --primary-glow: rgba(91,122,245,0.22);
                --cyan: #06b6d4;
                --cyan-dim: rgba(6,182,212,0.1);
                --success: #10b981;
                --success-dim: rgba(16,185,129,0.12);
                --success-glow: rgba(16,185,129,0.25);
                --warning: #f59e0b;
                --warning-dim: rgba(245,158,11,0.12);
                --danger: #f43f5e;
                --danger-dim: rgba(244,63,94,0.12);
                --danger-glow: rgba(244,63,94,0.2);
                --gold: #fbbf24;
                --gold-dim: rgba(251,191,36,0.15);
                --text-primary: #eaf0ff;
                --text-secondary: #8896b8;
                --text-muted: #404d6b;
                --radius-sm: 8px;
                --radius-md: 12px;
                --radius-lg: 16px;
                --radius-xl: 20px;
            }

            .stApp {
                background: var(--bg-base);
                color: var(--text-primary);
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            }

            .block-container {
                padding: 0.5rem 1rem 2rem;
                max-width: 100%;
            }

            /* ── TOP BAR ── */
            .pq-topbar {
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 0.5rem 1rem;
                padding: 0.6rem 0 0.75rem;
                margin-bottom: 0.5rem;
                border-bottom: 1px solid var(--border-subtle);
            }
            .pq-topbar-left {
                display: flex;
                align-items: center;
                gap: 0.75rem;
            }
            .pq-topbar-logo {
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }
            .pq-topbar-brand {
                font-size: 1.1rem;
                font-weight: 900;
                letter-spacing: -0.03em;
                background: linear-gradient(135deg, #a5b4fc 0%, #818cf8 50%, #6366f1 100%);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            .pq-topbar-version {
                font-size: 0.65rem;
                font-weight: 700;
                color: var(--text-muted);
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: 4px;
                padding: 0.1rem 0.35rem;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-topbar-meta {
                font-size: 0.72rem;
                font-weight: 500;
                color: var(--text-muted);
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }
            .pq-live-dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: var(--success);
                box-shadow: 0 0 6px var(--success);
                display: inline-block;
                animation: pulse-dot 2s ease-in-out infinite;
            }
            @keyframes pulse-dot {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.6; transform: scale(0.8); }
            }

            /* ── STATS STRIP ── */
            .pq-stats-strip {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 0.6rem;
                margin-bottom: 0.85rem;
            }
            @media (max-width: 640px) {
                .pq-stats-strip { grid-template-columns: repeat(2, 1fr); }
            }
            .pq-stat-tile {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.7rem 0.85rem;
                position: relative;
                overflow: hidden;
            }
            .pq-stat-tile::before {
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0;
                height: 2px;
                background: var(--tile-accent, var(--primary));
                border-radius: 2px 2px 0 0;
            }
            .pq-stat-tile-label {
                font-size: 0.62rem;
                font-weight: 700;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.07em;
                margin-bottom: 0.25rem;
            }
            .pq-stat-tile-value {
                font-size: 1.25rem;
                font-weight: 900;
                color: var(--text-primary);
                line-height: 1.1;
                font-family: 'JetBrains Mono', monospace;
                letter-spacing: -0.02em;
            }
            .pq-stat-tile-sub {
                font-size: 0.65rem;
                color: var(--text-muted);
                margin-top: 0.15rem;
            }

            /* ── HOW TO USE BANNER ── */
            .pq-onboard-strip {
                background: linear-gradient(135deg, rgba(91,122,245,0.1) 0%, rgba(6,182,212,0.06) 100%);
                border: 1px solid rgba(91,122,245,0.25);
                border-radius: var(--radius-md);
                padding: 0.7rem 1rem;
                margin-bottom: 0.85rem;
                display: flex;
                align-items: flex-start;
                gap: 0.65rem;
            }
            .pq-onboard-icon {
                font-size: 1.3rem;
                line-height: 1;
                flex-shrink: 0;
                margin-top: 0.05rem;
            }
            .pq-onboard-text {
                font-size: 0.8rem;
                color: var(--text-secondary);
                line-height: 1.5;
            }
            .pq-onboard-text strong {
                color: var(--text-primary);
                font-weight: 700;
            }
            .pq-onboard-steps {
                display: flex;
                gap: 0.5rem;
                flex-wrap: wrap;
                margin-top: 0.45rem;
            }
            .pq-onboard-step {
                background: var(--primary-dim);
                border: 1px solid rgba(91,122,245,0.3);
                border-radius: 999px;
                padding: 0.2rem 0.65rem;
                font-size: 0.72rem;
                font-weight: 600;
                color: #a5b4fc;
            }

            /* ── TABS ── */
            .stTabs [data-baseweb="tab-list"] {
                gap: 4px;
                background: transparent;
                border-bottom: 1px solid var(--border-subtle);
                padding-bottom: 0;
                flex-wrap: nowrap !important;
                overflow-x: auto !important;
                -webkit-overflow-scrolling: touch;
            }
            .stTabs [data-baseweb="tab"] {
                background: transparent;
                color: var(--text-muted);
                font-weight: 600;
                font-size: 0.82rem;
                padding: 8px 14px;
                border-radius: 8px 8px 0 0;
                border: none;
                white-space: nowrap !important;
                flex-shrink: 0 !important;
                transition: color 0.15s;
            }
            .stTabs [data-baseweb="tab"]:hover {
                color: var(--text-secondary) !important;
            }
            .stTabs [aria-selected="true"] {
                color: var(--primary) !important;
                background: rgba(91,122,245,0.08) !important;
                border-bottom: 2px solid var(--primary) !important;
            }
            .stTabs [data-baseweb="tab-panel"] {
                padding-top: 1rem;
            }

            /* ── CARDS ── */
            .pq-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.9rem 1rem;
                margin-bottom: 0.55rem;
            }
            .pq-card-compound {
                border-color: rgba(16,185,129,0.4);
                background: linear-gradient(135deg, var(--success-dim) 0%, var(--bg-surface) 60%);
                box-shadow: 0 0 24px var(--success-glow);
            }
            .pq-card-title {
                font-size: 0.92rem;
                font-weight: 700;
                color: var(--text-primary);
                line-height: 1.35;
                margin: 0 0 0.55rem;
            }
            .pq-card-row {
                display: flex;
                flex-wrap: wrap;
                gap: 0.45rem;
                align-items: center;
            }

            /* ── BADGES ── */
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
                background: var(--success-dim);
                color: var(--success);
                border: 1px solid rgba(16,185,129,0.4);
            }
            .pq-badge-blue {
                background: var(--primary-dim);
                color: var(--primary);
                border: 1px solid rgba(91,122,245,0.35);
            }
            .pq-badge-cyan {
                background: var(--cyan-dim);
                color: var(--cyan);
                border: 1px solid rgba(6,182,212,0.35);
            }
            .pq-badge-grey {
                background: var(--bg-elevated);
                color: var(--text-muted);
                border: 1px solid var(--border-default);
            }
            .pq-badge-red {
                background: var(--danger-dim);
                color: var(--danger);
                border: 1px solid rgba(244,63,94,0.35);
            }
            .pq-badge-gold {
                background: var(--gold-dim);
                color: var(--gold);
                border: 1px solid rgba(251,191,36,0.4);
            }
            .pq-stat {
                font-size: 0.78rem;
                color: var(--text-secondary);
            }
            .pq-stat strong {
                color: var(--text-primary);
                font-weight: 700;
            }

            /* ── TAB SECTION HEADERS ── */
            .pq-tab-header {
                margin-bottom: 0.85rem;
            }
            .pq-tab-title {
                font-size: 1.3rem;
                font-weight: 800;
                color: var(--text-primary);
                letter-spacing: -0.02em;
                margin: 0 0 0.25rem;
                line-height: 1.2;
            }
            .pq-tab-subtitle {
                font-size: 0.82rem;
                color: var(--text-secondary);
                margin: 0;
                line-height: 1.5;
            }
            .pq-tab-subtitle strong {
                color: var(--text-primary);
            }
            .pq-how-to-box {
                background: linear-gradient(135deg, rgba(91,122,245,0.07) 0%, transparent 100%);
                border: 1px solid rgba(91,122,245,0.18);
                border-radius: var(--radius-sm);
                padding: 0.55rem 0.75rem;
                margin-top: 0.55rem;
                font-size: 0.77rem;
                color: var(--text-secondary);
                line-height: 1.5;
            }
            .pq-how-to-box span { color: #a5b4fc; font-weight: 700; }

            /* ── VERDICT CONTAINERS ── */
            .pq-verdict-play {
                background: linear-gradient(135deg, rgba(16,185,129,0.2) 0%, rgba(5,150,105,0.08) 100%);
                border: 2px solid var(--success);
                border-radius: var(--radius-lg);
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
                box-shadow: 0 0 32px var(--success-glow);
            }
            .pq-verdict-play h2 {
                margin: 0 0 0.35rem;
                font-size: 1.35rem;
                font-weight: 800;
                color: var(--success);
            }
            .pq-verdict-play p {
                margin: 0;
                font-size: 0.95rem;
                color: var(--text-secondary);
                line-height: 1.5;
            }
            .pq-verdict-pass {
                background: var(--bg-surface);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-lg);
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
            }
            .pq-verdict-pass h2 {
                margin: 0 0 0.35rem;
                font-size: 1.2rem;
                font-weight: 800;
                color: var(--text-secondary);
            }
            .pq-verdict-pass p {
                margin: 0;
                font-size: 0.88rem;
                color: var(--text-muted);
            }

            /* ── ARB SPLIT ── */
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
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-md);
                padding: 0.85rem;
                text-align: center;
            }
            .pq-split-side .venue {
                font-size: 0.65rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.35rem;
            }
            .pq-split-side .leg {
                font-size: 1rem;
                font-weight: 800;
                color: var(--primary);
            }
            .pq-arb-banner {
                background: linear-gradient(90deg, rgba(16,185,129,0.25), rgba(5,150,105,0.1));
                border: 2px solid var(--success);
                border-radius: var(--radius-md);
                padding: 1rem 1.1rem;
                text-align: center;
                margin-top: 0.65rem;
            }
            .pq-arb-banner h3 {
                margin: 0 0 0.25rem;
                color: var(--success);
                font-size: 1.05rem;
                font-weight: 800;
            }
            .pq-arb-banner p {
                margin: 0;
                color: var(--text-secondary);
                font-size: 0.9rem;
            }

            /* ── WARNING BANNER ── */
            .pq-trap-banner {
                background: linear-gradient(135deg, var(--danger-dim), rgba(153,27,27,0.08));
                border: 2px solid var(--danger);
                border-radius: var(--radius-md);
                padding: 1.1rem 1.2rem;
                margin-top: 0.75rem;
            }
            .pq-trap-banner h3 {
                margin: 0 0 0.4rem;
                color: var(--danger);
                font-size: 1rem;
                font-weight: 800;
            }
            .pq-trap-banner p {
                margin: 0;
                color: var(--text-secondary);
                font-size: 0.88rem;
                line-height: 1.45;
            }

            /* ── INPUT CARD ── */
            .pq-input-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.85rem 1rem 0.25rem;
                margin-bottom: 0.75rem;
            }

            /* ── STREAMLIT WIDGETS ── */
            [data-testid="stMetric"] {
                background: var(--bg-surface) !important;
                border: 1px solid var(--border-subtle) !important;
                border-radius: var(--radius-md) !important;
                padding: 0.75rem 0.9rem !important;
            }
            [data-testid="stMetricValue"] {
                color: var(--text-primary) !important;
                font-family: 'JetBrains Mono', monospace !important;
            }
            [data-testid="stMetricLabel"] {
                color: var(--text-muted) !important;
                font-size: 0.72rem !important;
                font-weight: 700 !important;
                text-transform: uppercase !important;
                letter-spacing: 0.06em !important;
            }
            [data-testid="stDataFrame"] {
                border: 1px solid var(--border-subtle) !important;
                border-radius: var(--radius-md) !important;
            }
            [data-testid="stMetricContainer"] {
                background: var(--bg-surface) !important;
                border: 1px solid var(--border-subtle) !important;
                border-radius: var(--radius-md) !important;
                padding: 10px 15px !important;
            }
            .stSlider label, .stNumberInput label, .stSelectbox label {
                font-weight: 600 !important;
                font-size: 0.82rem !important;
                color: var(--text-secondary) !important;
            }
            .stTextInput input, .stNumberInput input {
                background: var(--bg-input) !important;
                border: 1px solid var(--border-default) !important;
                border-radius: var(--radius-sm) !important;
                color: var(--text-primary) !important;
                font-size: 0.9rem !important;
            }
            .stTextInput input:focus, .stNumberInput input:focus {
                border-color: var(--primary) !important;
                box-shadow: 0 0 0 2px var(--primary-glow) !important;
            }
            .stSelectbox > div > div {
                background: var(--bg-input) !important;
                border: 1px solid var(--border-default) !important;
                border-radius: var(--radius-sm) !important;
                color: var(--text-primary) !important;
            }
            hr {
                border-color: var(--border-subtle);
                margin: 0.75rem 0;
            }
            div.stExpander {
                background: var(--bg-surface) !important;
                border: 1px solid var(--border-subtle) !important;
                border-radius: var(--radius-md) !important;
            }

            /* ── SECTION LABELS & PICKERS ── */
            .pq-section-label {
                font-size: 0.68rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.1em;
                margin: 0.85rem 0 0.4rem;
                display: flex;
                align-items: center;
                gap: 0.4rem;
            }
            .pq-section-label::after {
                content: '';
                flex: 1;
                height: 1px;
                background: var(--border-subtle);
            }
            .pq-pick-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-sm);
                padding: 0.55rem 0.75rem;
                margin-bottom: 0.25rem;
                transition: border-color 0.15s;
            }
            .pq-pick-selected {
                border-color: var(--primary);
                background: var(--primary-dim);
            }
            .pq-pick-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 600;
                color: var(--text-primary);
                line-height: 1.35;
            }
            .pq-pick-meta {
                display: block;
                font-size: 0.72rem;
                color: var(--primary);
                font-weight: 700;
                margin-top: 0.15rem;
            }
            .pq-page-indicator {
                text-align: center;
                font-size: 0.75rem;
                color: var(--text-muted);
                margin: 0.35rem 0 0;
            }
            .pq-selected-banner {
                background: var(--primary-dim);
                border: 1px solid rgba(91,122,245,0.3);
                border-radius: var(--radius-sm);
                padding: 0.65rem 0.8rem;
                font-size: 0.78rem;
                color: #a5b4fc;
                line-height: 1.4;
                margin: 0.5rem 0 0.75rem;
            }
            .pq-odds-bar {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.55rem 0.75rem 0.35rem;
                margin-bottom: 0.65rem;
            }

            /* ── BUTTONS ── */
            .stButton > button {
                border-radius: var(--radius-sm) !important;
                font-weight: 600 !important;
                min-height: 2.35rem;
                font-size: 0.84rem !important;
                transition: all 0.15s !important;
            }
            .stButton > button[kind="secondary"] {
                background: var(--bg-elevated) !important;
                border: 1px solid var(--border-default) !important;
                color: var(--text-secondary) !important;
            }
            .stButton > button[kind="secondary"]:hover {
                border-color: var(--primary) !important;
                color: var(--text-primary) !important;
            }
            .stButton > button[kind="primary"] {
                background: linear-gradient(135deg, #5b7af5 0%, #4f46e5 100%) !important;
                border: 1px solid rgba(91,122,245,0.5) !important;
                color: #fff !important;
                box-shadow: 0 2px 8px rgba(91,122,245,0.25) !important;
            }
            .stButton > button[kind="primary"]:hover {
                box-shadow: 0 4px 16px rgba(91,122,245,0.4) !important;
                transform: translateY(-1px) !important;
            }

            /* ── SEGMENTED CONTROL ── */
            [data-testid="stSegmentedControl"] {
                background: var(--bg-surface);
                border-radius: var(--radius-sm);
                padding: 3px;
                border: 1px solid var(--border-subtle);
            }

            /* ── EXPLORE FEED ── */
            .pq-search-hero {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.65rem 0.85rem;
                margin-bottom: 0.55rem;
            }
            .pq-feed-row {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.8rem 0.9rem;
                margin-bottom: 0.4rem;
                transition: border-color 0.15s, box-shadow 0.15s;
            }
            .pq-feed-row:hover {
                border-color: var(--border-emphasis);
                box-shadow: 0 2px 12px rgba(0,0,0,0.3);
            }
            .pq-feed-meta {
                display: block;
                font-size: 0.62rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.25rem;
            }
            .pq-feed-title {
                display: block;
                font-size: 0.9rem;
                font-weight: 700;
                color: var(--text-primary);
                line-height: 1.35;
            }
            .pq-feed-event {
                display: block;
                font-size: 0.72rem;
                color: var(--text-muted);
                margin-top: 0.2rem;
            }
            .pq-odd-pill {
                display: block;
                text-align: center;
                padding: 0.5rem 0.35rem;
                border-radius: var(--radius-sm);
                font-weight: 800;
                font-size: 0.9rem;
            }
            .pq-odd-yes {
                background: rgba(91,122,245,0.12);
                color: var(--primary);
                border: 1px solid rgba(91,122,245,0.3);
            }
            .pq-odd-no {
                background: var(--bg-elevated);
                color: var(--text-secondary);
                border: 1px solid var(--border-default);
            }

            /* ── VALUE PLAY CARDS ── */
            .pq-value-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
                padding: 1.1rem 1.2rem;
                margin-bottom: 0.65rem;
                position: relative;
                overflow: hidden;
            }
            .pq-value-card::before {
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0;
                height: 3px;
                background: var(--card-accent, var(--success));
            }
            .pq-value-card-hot {
                border-color: rgba(16,185,129,0.35);
                box-shadow: 0 0 24px rgba(16,185,129,0.1);
            }
            .pq-value-card-elite {
                border: 2px solid var(--success);
                box-shadow: 0 0 28px rgba(16,185,129,0.2);
            }
            .pq-value-card-elite::before {
                background: linear-gradient(90deg, var(--gold), var(--success));
                height: 3px;
            }
            .pq-rank-badge {
                display: inline-flex;
                align-items: center;
                gap: 0.3rem;
                background: var(--success-dim);
                color: var(--success);
                border: 1px solid rgba(16,185,129,0.4);
                font-weight: 800;
                font-size: 0.72rem;
                padding: 0.28rem 0.65rem;
                border-radius: 999px;
                margin-bottom: 0.55rem;
                letter-spacing: 0.03em;
            }
            .pq-rank-badge-elite {
                background: linear-gradient(135deg, var(--gold-dim), var(--success-dim));
                color: var(--gold);
                border-color: rgba(251,191,36,0.5);
                font-size: 0.78rem;
            }
            .pq-event-name {
                font-size: 0.98rem;
                font-weight: 800;
                color: var(--text-primary);
                margin: 0 0 0.65rem;
                line-height: 1.4;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(135deg, var(--primary) 0%, #4338ca 100%);
                color: #fff;
                font-weight: 800;
                font-size: 0.82rem;
                padding: 0.45rem 1rem;
                border-radius: 999px;
                margin-bottom: 0.6rem;
                letter-spacing: 0.02em;
                box-shadow: 0 2px 10px rgba(91,122,245,0.3);
            }
            .pq-ev-badge {
                display: inline-block;
                background: var(--success-dim);
                color: var(--success);
                border: 1px solid rgba(16,185,129,0.4);
                font-weight: 800;
                font-size: 0.8rem;
                padding: 0.3rem 0.7rem;
                border-radius: var(--radius-sm);
            }
            .pq-metric-row {
                display: flex;
                gap: 1rem;
                flex-wrap: wrap;
                font-size: 0.78rem;
                color: var(--text-secondary);
            }
            .pq-metric-row strong { color: var(--text-primary); }

            /* ── PROBABILITY BAR ── */
            .pq-prob-bar-wrap {
                margin: 0.55rem 0;
            }
            .pq-prob-bar-label {
                display: flex;
                justify-content: space-between;
                font-size: 0.68rem;
                font-weight: 700;
                color: var(--text-muted);
                margin-bottom: 0.2rem;
            }
            .pq-prob-bar-track {
                height: 6px;
                background: var(--bg-elevated);
                border-radius: 99px;
                overflow: hidden;
            }
            .pq-prob-bar-fill {
                height: 100%;
                border-radius: 99px;
                background: var(--bar-color, var(--primary));
                transition: width 0.5s ease;
            }

            /* ── AUDIT BANNERS ── */
            .pq-banner-play {
                background: linear-gradient(135deg, rgba(16,185,129,0.25), rgba(5,150,105,0.1));
                border: 2px solid var(--success);
                border-radius: var(--radius-md);
                padding: 1.4rem;
                text-align: center;
                font-size: 1.4rem;
                font-weight: 900;
                color: var(--success);
                margin-top: 1rem;
                letter-spacing: 0.03em;
                box-shadow: 0 0 32px rgba(16,185,129,0.15);
            }
            .pq-banner-pass {
                background: var(--danger-dim);
                border: 2px solid rgba(244,63,94,0.4);
                border-radius: var(--radius-md);
                padding: 1.4rem;
                text-align: center;
                font-size: 1.3rem;
                font-weight: 900;
                color: var(--text-muted);
                margin-top: 1rem;
            }

            /* ── HYPE VS REALITY ── */
            .pq-hype-col {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 1.1rem;
                text-align: center;
            }
            .pq-hype-val {
                font-size: 2.5rem;
                font-weight: 900;
                color: var(--text-primary);
                font-family: 'JetBrains Mono', monospace;
                line-height: 1;
                margin: 0.25rem 0;
            }
            .pq-hype-label {
                font-size: 0.68rem;
                font-weight: 700;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.07em;
            }
            .pq-bubble-badge {
                background: linear-gradient(135deg, rgba(245,158,11,0.25), rgba(239,68,68,0.12));
                border: 2px solid var(--warning);
                color: var(--warning);
                font-weight: 800;
                font-size: 1rem;
                padding: 1rem 1.2rem;
                border-radius: var(--radius-md);
                text-align: center;
                margin-top: 1rem;
                box-shadow: 0 0 24px rgba(245,158,11,0.15);
            }
            .pq-sentiment-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-lg);
                padding: 1.25rem;
                margin-top: 0.85rem;
            }
            .pq-sentiment-delta {
                text-align: center;
                font-size: 3rem;
                font-weight: 900;
                line-height: 1;
                margin: 0.5rem 0 0.25rem;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-sentiment-delta.positive { color: var(--warning); }
            .pq-sentiment-delta.negative { color: var(--cyan); }
            .pq-sentiment-delta.neutral { color: var(--text-muted); }
            .pq-sentiment-verdict {
                text-align: center;
                font-size: 0.88rem;
                font-weight: 600;
                color: var(--text-secondary);
                margin: 0;
            }

            /* ── ARB RECIPE ── */
            .pq-recipe {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
                padding: 1rem 1.15rem;
                margin: 0.5rem 0;
            }
            .pq-recipe-step {
                font-size: 0.92rem;
                color: var(--text-secondary);
                margin: 0.45rem 0;
                line-height: 1.5;
            }
            .pq-recipe-step strong { color: var(--primary); }
            .pq-lock-banner {
                background: linear-gradient(135deg, rgba(16,185,129,0.2), rgba(5,150,105,0.08));
                border: 2px solid var(--success);
                border-radius: var(--radius-md);
                padding: 1rem;
                text-align: center;
                font-size: 1.1rem;
                font-weight: 800;
                color: var(--success);
                margin-top: 0.75rem;
                box-shadow: 0 0 20px rgba(16,185,129,0.12);
            }

            /* ── CROSS-BOOK COMPARISON ── */
            .pq-arb-compare {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
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
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-md);
                padding: 0.9rem;
            }
            .pq-book-header {
                font-size: 0.65rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.1em;
                margin-bottom: 0.4rem;
                display: flex;
                align-items: center;
                gap: 0.35rem;
            }
            .pq-book-badge {
                background: var(--primary-dim);
                color: var(--primary);
                border-radius: 4px;
                padding: 0.1rem 0.3rem;
                font-size: 0.6rem;
                font-weight: 700;
            }
            .pq-book-title {
                font-size: 0.82rem;
                font-weight: 700;
                color: var(--text-primary);
                line-height: 1.35;
                margin-bottom: 0.65rem;
                min-height: 2.2rem;
            }
            .pq-odd-row {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 0.45rem 0.55rem;
                border-radius: var(--radius-sm);
                margin-bottom: 0.35rem;
                font-size: 0.8rem;
                font-weight: 700;
            }
            .pq-odd-row.yes {
                background: var(--primary-dim);
                border: 1px solid rgba(91,122,245,0.3);
                color: var(--primary);
            }
            .pq-odd-row.no {
                background: var(--bg-base);
                border: 1px solid var(--border-subtle);
                color: var(--text-secondary);
            }
            .pq-odd-row .pq-odd-val {
                font-weight: 800;
                color: var(--text-primary);
                font-size: 0.78rem;
                font-family: 'JetBrains Mono', monospace;
            }

            /* ── STRATEGY CARDS ── */
            .pq-strategy-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-lg);
                padding: 1.1rem 1.15rem;
                margin: 0.65rem 0;
            }
            .pq-strategy-card.pq-strategy-live {
                border-color: rgba(16,185,129,0.45);
                box-shadow: 0 0 24px rgba(16,185,129,0.12);
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
                color: var(--text-primary);
                margin: 0;
            }
            .pq-strategy-badge {
                font-size: 0.67rem;
                font-weight: 800;
                padding: 0.25rem 0.6rem;
                border-radius: 999px;
                text-transform: uppercase;
                letter-spacing: 0.04em;
            }
            .pq-strategy-badge.live {
                background: var(--success-dim);
                color: var(--success);
                border: 1px solid rgba(16,185,129,0.4);
            }
            .pq-strategy-badge.dead {
                background: var(--bg-elevated);
                color: var(--text-muted);
                border: 1px solid var(--border-default);
            }
            .pq-strategy-metrics {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 0.5rem;
                margin-top: 0.65rem;
            }
            @media (max-width: 480px) {
                .pq-strategy-metrics { grid-template-columns: 1fr; }
            }
            .pq-metric-box {
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-sm);
                padding: 0.6rem 0.7rem;
                text-align: center;
            }
            .pq-metric-box .lbl {
                display: block;
                font-size: 0.6rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.06em;
            }
            .pq-metric-box .val {
                display: block;
                font-size: 1rem;
                font-weight: 800;
                color: var(--text-primary);
                margin-top: 0.15rem;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-metric-box .val.green { color: var(--success); }
            .pq-metric-box .val.red { color: var(--danger); }

            /* ── ARB DETAIL PANEL ── */
            .pq-arb-detail {
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-md);
                padding: 0.85rem;
                margin-top: 0.75rem;
            }
            .pq-arb-detail-title {
                margin: 0 0 0.5rem;
                color: var(--text-secondary);
                font-size: 0.72rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.08em;
            }
            .pq-arb-ticket-row {
                display: grid;
                grid-template-columns: 1.25fr 0.75fr 0.8fr 0.9fr;
                gap: 0.45rem;
                align-items: center;
                padding: 0.5rem 0;
                border-top: 1px solid var(--border-subtle);
                font-size: 0.78rem;
                color: var(--text-secondary);
            }
            .pq-arb-ticket-row.header {
                border-top: 0;
                padding-top: 0;
                color: var(--text-muted);
                font-size: 0.63rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.06em;
            }
            .pq-arb-ticket-row strong {
                color: var(--text-primary);
                font-weight: 800;
            }
            .pq-arb-ticket-row .cash {
                color: var(--primary);
                font-weight: 800;
                text-align: right;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-arb-explain {
                margin: 0.65rem 0 0;
                color: var(--text-secondary);
                font-size: 0.82rem;
                line-height: 1.5;
            }
            .pq-arb-explain strong { color: var(--text-primary); }
            .pq-arb-warning {
                background: var(--danger-dim);
                border: 1px solid rgba(244,63,94,0.35);
                border-radius: var(--radius-sm);
                color: #fda4af;
                font-size: 0.8rem;
                line-height: 1.45;
                margin-top: 0.7rem;
                padding: 0.7rem 0.8rem;
            }

            /* ── ARB SPOTLIGHT ── */
            .pq-arb-spotlight {
                background: linear-gradient(180deg, rgba(91,122,245,0.14), rgba(6,10,20,0.95));
                border: 2px solid var(--primary);
                border-radius: var(--radius-xl);
                box-shadow: 0 0 32px var(--primary-glow);
                margin: 0.75rem 0 1rem;
                padding: 1.1rem 1.2rem;
            }
            .pq-arb-spotlight.live {
                background: linear-gradient(180deg, rgba(16,185,129,0.18), rgba(6,10,20,0.95));
                border-color: var(--success);
                box-shadow: 0 0 32px var(--success-glow);
            }
            .pq-arb-spotlight.dead {
                background: linear-gradient(180deg, rgba(244,63,94,0.12), rgba(6,10,20,0.95));
                border-color: rgba(244,63,94,0.5);
                box-shadow: 0 0 24px var(--danger-glow);
            }
            .pq-arb-spotlight-kicker {
                color: var(--text-muted);
                font-size: 0.65rem;
                font-weight: 900;
                letter-spacing: 0.1em;
                margin: 0 0 0.3rem;
                text-transform: uppercase;
            }
            .pq-arb-spotlight-title {
                color: var(--text-primary);
                font-size: 1.15rem;
                font-weight: 900;
                letter-spacing: -0.02em;
                line-height: 1.2;
                margin: 0 0 0.75rem;
            }
            .pq-arb-action-list {
                display: grid;
                gap: 0.5rem;
                margin: 0.75rem 0;
            }
            .pq-arb-action {
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-md);
                padding: 0.8rem;
            }
            .pq-arb-action .step {
                color: var(--text-muted);
                display: block;
                font-size: 0.63rem;
                font-weight: 900;
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }
            .pq-arb-action .take {
                color: var(--text-primary);
                display: block;
                font-size: 0.96rem;
                font-weight: 900;
                margin-top: 0.15rem;
            }
            .pq-arb-action .meta {
                color: var(--primary);
                display: block;
                font-size: 0.78rem;
                font-weight: 700;
                margin-top: 0.2rem;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-arb-spotlight-note {
                color: var(--text-secondary);
                font-size: 0.82rem;
                line-height: 1.5;
                margin: 0.65rem 0 0;
            }
            .pq-arb-spotlight-note strong { color: var(--text-primary); }
            @media (max-width: 480px) {
                .pq-arb-ticket-row {
                    grid-template-columns: 1fr 0.62fr;
                    gap: 0.28rem 0.45rem;
                }
                .pq-arb-ticket-row.header { display: none; }
                .pq-arb-ticket-row .cash { text-align: left; }
            }

            /* ── KALSHI SUGGEST ── */
            .pq-suggest-card {
                background: var(--bg-elevated);
                border: 1px solid var(--border-default);
                border-radius: var(--radius-md);
                padding: 0.75rem 0.9rem;
                margin-bottom: 0.4rem;
            }
            .pq-suggest-score {
                display: inline-block;
                font-size: 0.63rem;
                font-weight: 800;
                color: var(--primary);
                background: var(--primary-dim);
                border: 1px solid rgba(91,122,245,0.3);
                border-radius: 999px;
                padding: 0.15rem 0.45rem;
                margin-bottom: 0.35rem;
            }
            .pq-suggest-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 700;
                color: var(--text-primary);
                line-height: 1.35;
            }
            .pq-suggest-meta {
                display: block;
                font-size: 0.72rem;
                color: var(--primary);
                font-weight: 600;
                margin-top: 0.2rem;
            }
            .pq-build-tag {
                color: var(--primary);
                font-weight: 700;
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.9em;
            }

            /* ── PERFORMANCE CALENDAR ── */
            .pq-perf-calendar {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 1rem 1.05rem 1.1rem;
                margin: 0.65rem 0 1rem;
            }
            .pq-perf-cal-header {
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                margin-bottom: 0.75rem;
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .pq-perf-cal-title {
                font-size: 1rem;
                font-weight: 800;
                color: var(--text-primary);
                letter-spacing: -0.02em;
            }
            .pq-perf-cal-sub {
                font-size: 0.7rem;
                font-weight: 600;
                color: var(--text-muted);
                margin-top: 0.1rem;
            }
            .pq-perf-cal-month-pnl {
                font-size: 0.9rem;
                font-weight: 800;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-perf-cal-month-pnl.pos { color: var(--success); }
            .pq-perf-cal-month-pnl.neg { color: var(--danger); }
            .pq-perf-cal-month-pnl.flat { color: var(--text-muted); }
            .pq-perf-cal-grid {
                display: grid;
                grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 5px;
            }
            .pq-perf-cal-head {
                text-align: center;
                font-size: 0.6rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.07em;
                padding: 0.2rem 0 0.4rem;
            }
            .pq-perf-cal-cell {
                min-height: 62px;
                border-radius: var(--radius-sm);
                border: 1px solid var(--border-subtle);
                background: var(--bg-elevated);
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
                box-shadow: 0 0 0 2px var(--primary);
                border-color: var(--primary);
            }
            .pq-perf-cal-cell.pq-perf-win {
                background: rgba(16,185,129,0.15);
                border-color: rgba(16,185,129,0.4);
            }
            .pq-perf-cal-cell.pq-perf-loss {
                background: rgba(244,63,94,0.12);
                border-color: rgba(244,63,94,0.35);
            }
            .pq-perf-cal-cell.pq-perf-flat {
                background: var(--bg-surface);
                border-color: var(--border-default);
            }
            .pq-perf-cal-day {
                font-size: 0.6rem;
                font-weight: 700;
                color: var(--text-muted);
                line-height: 1;
            }
            .pq-perf-cal-pnl {
                font-size: 0.7rem;
                font-weight: 800;
                text-align: center;
                line-height: 1.1;
                margin-top: 0.15rem;
                font-family: 'JetBrains Mono', monospace;
            }
            .pq-perf-cal-pnl.pos { color: var(--success); }
            .pq-perf-cal-pnl.neg { color: var(--danger); }
            .pq-perf-cal-pnl.flat { color: var(--text-secondary); }
            .pq-perf-cal-count {
                font-size: 0.56rem;
                font-weight: 600;
                color: var(--text-muted);
                text-align: center;
                margin-top: 0.1rem;
            }

            /* ── LEGACY CALENDAR (flexbox) ── */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid { display: flex; flex-wrap: wrap; gap: 4px; }
            .pq-cal-head {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                text-align: center;
                font-size: 0.65rem;
                font-weight: 700;
                color: var(--text-muted);
                padding: 0.25rem 0;
            }
            .pq-cal-cell {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                aspect-ratio: 1;
                border-radius: var(--radius-sm);
                border: 1px solid var(--border-subtle);
                position: relative;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .pq-cal-day {
                position: absolute;
                top: 4px; left: 6px;
                font-size: 0.62rem;
                color: var(--text-muted);
                font-weight: 600;
            }
            .pq-cal-neutral { background: var(--bg-surface); }
            .pq-cal-win { background: rgba(16,185,129,0.18); border-color: rgba(16,185,129,0.4); }
            .pq-cal-loss { background: rgba(244,63,94,0.14); border-color: rgba(244,63,94,0.35); }
            .pq-cal-pnl { font-size: 0.72rem; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
            .pq-cal-pnl.pos { color: var(--success); }
            .pq-cal-pnl.neg { color: var(--danger); }
            .pq-cal-dash { color: var(--border-emphasis); font-size: 0.85rem; }

            /* ── FEED COMPACT ── */
            .pq-feed-compact {
                display: flex; align-items: center; justify-content: space-between;
                gap: 0.65rem; flex-wrap: wrap;
            }
            .pq-feed-body { flex: 1 1 200px; min-width: 0; }
            .pq-feed-odds { display: flex; gap: 0.35rem; flex-shrink: 0; }
            .pq-odd-pill.sm {
                padding: 0.35rem 0.5rem; font-size: 0.78rem;
                border-radius: var(--radius-sm); white-space: nowrap;
            }

            /* ── KPI ROW ── */
            .pq-kpi-row {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 0.65rem;
                margin-bottom: 1rem;
            }
            @media (max-width: 640px) {
                .pq-kpi-row { grid-template-columns: 1fr; }
            }
            .pq-kpi-card {
                background: var(--bg-surface);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius-md);
                padding: 0.8rem 1rem;
                text-align: center;
            }
            .pq-kpi-label {
                font-size: 0.65rem;
                font-weight: 800;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.35rem;
            }
            .pq-kpi-value {
                font-size: 1.6rem;
                font-weight: 900;
                font-family: 'JetBrains Mono', monospace;
                line-height: 1.05;
            }
            .pq-kpi-value.profit { color: var(--success); }
            .pq-kpi-value.loss { color: var(--danger); }
            .pq-kpi-value.neutral { color: var(--text-primary); }

            /* ── DEPLOY STRIP ── */
            .pq-deploy-strip {
                background: linear-gradient(135deg, rgba(16,185,129,0.08), rgba(6,182,212,0.05));
                border: 1px solid rgba(16,185,129,0.2);
                border-radius: var(--radius-sm);
                padding: 0.35rem 0.7rem;
                margin-bottom: 0.5rem;
                font-size: 0.72rem;
                color: var(--text-muted);
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }
            .pq-deploy-live {
                color: var(--success);
                font-weight: 800;
                letter-spacing: 0.04em;
                font-size: 0.68rem;
                text-transform: uppercase;
            }

            .block-container { max-width: 1200px; padding-bottom: 2.5rem; }

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
    edge_gap = model_win - implied_pct
    card_cls = "pq-value-card pq-value-card-elite" if rank == 1 else "pq-value-card pq-value-card-hot"
    rank_cls = "pq-rank-badge pq-rank-badge-elite" if rank == 1 else "pq-rank-badge"
    rank_icon = "🥇" if rank == 1 else f"#{rank}"
    rank_label = f"{rank_icon} Best Play" if rank == 1 else f"{rank_icon} Edge Play"
    event = html.escape(str(row["Question"]))
    vol_raw = _coerce_float(row.get("Volume")) or 0.0
    vol_fmt = f"${vol_raw/1000:.0f}K" if vol_raw >= 1000 else f"${vol_raw:.0f}"
    model_bar_w = int(model_win)
    implied_bar_w = int(implied_pct)
    st.markdown(
        f"""
        <div class="{card_cls}">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:0.55rem;">
                <span class="{rank_cls}">{rank_label}</span>
                <span class="pq-ev-badge">+{net_edge:.1f}% NET EV</span>
            </div>
            <p class="pq-event-name">{event}</p>
            <div style="margin-bottom:0.65rem;">
                <div class="pq-cta-pill">BET NO @ {implied_pct:.0f}¢</div>
            </div>
            <div class="pq-prob-bar-wrap">
                <div class="pq-prob-bar-label">
                    <span>True Win Probability</span>
                    <span style="color:#eaf0ff;font-family:'JetBrains Mono',monospace;">{model_win:.1f}%</span>
                </div>
                <div class="pq-prob-bar-track">
                    <div class="pq-prob-bar-fill" style="width:{model_bar_w}%;--bar-color:#10b981;"></div>
                </div>
            </div>
            <div class="pq-prob-bar-wrap">
                <div class="pq-prob-bar-label">
                    <span>Market Implied</span>
                    <span style="color:#8896b8;font-family:'JetBrains Mono',monospace;">{implied_pct:.1f}%</span>
                </div>
                <div class="pq-prob-bar-track">
                    <div class="pq-prob-bar-fill" style="width:{implied_bar_w}%;--bar-color:#5b7af5;"></div>
                </div>
            </div>
            <div class="pq-metric-row" style="margin-top:0.55rem;">
                <span>Edge gap <strong style="color:#10b981;">+{edge_gap:.1f}%</strong></span>
                <span>Volume <strong>{vol_fmt}</strong></span>
                <span>No cost <strong style="font-family:'JetBrains Mono',monospace;">{implied_pct:.1f}¢</strong></span>
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
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">🔥 Top Value Plays</h2>
            <p class="pq-tab-subtitle">
                Mathematically profitable NO-side bets on Polymarket, filtered by our quant engine.<br>
                <strong>Only shows plays with &gt;75% true win probability and ≥5% net EV edge after fees.</strong>
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Each card shows a market where the true probability exceeds
                what the market is pricing in. The edge gap is your advantage — the wider, the better.
                Buy NO at the listed price on Polymarket to capture the edge.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
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
            <div class="pq-value-card" style="text-align:center;padding:2.5rem 1.5rem;">
                <div style="font-size:2.5rem;margin-bottom:0.75rem;">📊</div>
                <p class="pq-event-name" style="margin-bottom:0.5rem;">No qualifying edges right now</p>
                <p style="color:#8896b8;font-size:0.88rem;line-height:1.6;margin:0;max-width:380px;margin:0 auto;">
                    No markets currently clear all thresholds. Markets refresh every 60s — check back soon
                    or adjust your search filters.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    n = len(df)
    st.markdown(
        f'<p class="pq-section-label">{n} elite edge{"s" if n != 1 else ""} on slate</p>',
        unsafe_allow_html=True,
    )
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
    edge_color = "#10b981" if ev_yield_pct >= 5.0 else "#f59e0b" if ev_yield_pct > 0 else "#f43f5e"
    ev_bar_w = max(0, min(100, abs(ev_yield_pct) * 5))
    ev_bar_color = "#10b981" if ev_yield_pct >= 0 else "#f43f5e"
    prob_bar_w = int(true_win_prob)
    market_implied = share_price

    m1, m2, m3 = st.columns(3)
    m1.metric("True Win Probability", f"{true_win_prob:.1f}%",
              delta=f"{true_win_prob - market_implied:+.1f}% vs market")
    m2.metric("Quantitative Edge", edge_display)
    m3.metric("Kelly Allocation", f"{kelly_pct:.1f}% of bankroll")

    st.markdown(
        f"""
        <div class="pq-card" style="margin-top:0.75rem;">
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;">
                <div>
                    <div class="pq-prob-bar-wrap">
                        <div class="pq-prob-bar-label">
                            <span>Your True Probability</span>
                            <span style="color:#eaf0ff;font-family:'JetBrains Mono',monospace;font-weight:800;">{true_win_prob:.1f}%</span>
                        </div>
                        <div class="pq-prob-bar-track" style="height:10px;">
                            <div class="pq-prob-bar-fill" style="width:{prob_bar_w}%;--bar-color:#10b981;"></div>
                        </div>
                    </div>
                    <div class="pq-prob-bar-wrap" style="margin-top:0.65rem;">
                        <div class="pq-prob-bar-label">
                            <span>Market Implied</span>
                            <span style="color:#8896b8;font-family:'JetBrains Mono',monospace;">{market_implied:.1f}%</span>
                        </div>
                        <div class="pq-prob-bar-track" style="height:10px;">
                            <div class="pq-prob-bar-fill" style="width:{int(market_implied)}%;--bar-color:#5b7af5;"></div>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="pq-prob-bar-wrap">
                        <div class="pq-prob-bar-label">
                            <span>EV Edge Strength</span>
                            <span style="color:{edge_color};font-family:'JetBrains Mono',monospace;font-weight:800;">{edge_display}</span>
                        </div>
                        <div class="pq-prob-bar-track" style="height:10px;">
                            <div class="pq-prob-bar-fill" style="width:{ev_bar_w}%;--bar-color:{ev_bar_color};"></div>
                        </div>
                    </div>
                    <div style="margin-top:0.65rem;display:flex;gap:0.5rem;flex-wrap:wrap;">
                        <span class="pq-badge {'pq-badge-green' if prob_ok else 'pq-badge-red'}">{'✓' if prob_ok else '✗'} Prob Gate ≥{WIN_PROB_THRESHOLD:.0f}%</span>
                        <span class="pq-badge {'pq-badge-green' if ev_ok else 'pq-badge-red'}">{'✓' if ev_ok else '✗'} EV Gate ≥{EV_THRESHOLD:.1f}%</span>
                    </div>
                </div>
            </div>
            <div style="margin-top:0.85rem;padding-top:0.75rem;border-top:1px solid var(--border-subtle);display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;text-align:center;">
                <div>
                    <div style="font-size:0.62rem;font-weight:800;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.07em;">Projected EV</div>
                    <div style="font-size:1.2rem;font-weight:900;color:{edge_color};font-family:'JetBrains Mono',monospace;">${ev_dollars:+,.2f}</div>
                </div>
                <div>
                    <div style="font-size:0.62rem;font-weight:800;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.07em;">Stake</div>
                    <div style="font-size:1.2rem;font-weight:900;color:var(--text-primary);font-family:'JetBrains Mono',monospace;">${stake:,.0f}</div>
                </div>
                <div>
                    <div style="font-size:0.62rem;font-weight:800;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.07em;">Kelly Size</div>
                    <div style="font-size:1.2rem;font-weight:900;color:var(--text-primary);font-family:'JetBrains Mono',monospace;">{kelly_pct:.1f}%</div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if ev_yield_pct >= 5.0:
        st.success("✅ STRONG VALUE EDGE — Line clears both model thresholds. Size within Kelly discipline and execute if liquidity supports.")
    elif ev_yield_pct > 0.0:
        st.warning("⚠️ MARGINAL EDGE — Positive EV but below the 5% strong-value band. Consider sizing down or waiting for a better line.")
    else:
        st.error("⛔ NO MATHEMATICAL EDGE — Market price exceeds model fair value. Pass and preserve bankroll until the line moves.")

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
    with st.expander("🔍 View Full Quant Rationale & Settlement Math", expanded=False):
        st.markdown(rationale)


def render_audit_my_bet() -> None:
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">⚖️ Check My Bet</h2>
            <p class="pq-tab-subtitle">
                Enter your own win estimate and the market price to get an instant edge analysis.
                <strong>Tells you exactly whether to bet, how much, and why.</strong>
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Enter your true win probability (your estimate),
                the share price in cents, and your stake. The calculator shows projected EV,
                Kelly allocation, and a clear pass/play verdict.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        true_win_prob = st.number_input(
            "Your true win probability (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
            help="Your best estimate of the actual win probability (not the market price)",
        )
    with col2:
        share_price = st.number_input(
            "Market price / share price (¢)",
            min_value=0.01,
            max_value=99.99,
            value=50.0,
            step=1.0,
            help="The current price in cents — this is what you'd pay per share",
        )
    c1, c2, c3 = st.columns(3)
    with c1:
        stake = st.number_input(
            "Your stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
            help="Total dollars you plan to wager",
        )
    with c2:
        st.markdown(
            f'<div style="padding-top:1.5rem;font-size:0.78rem;color:var(--text-muted);">'
            f'Win gate: >{WIN_PROB_THRESHOLD:.0f}% · EV gate: ≥{EV_THRESHOLD:.1f}%</div>',
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    ev_dollars, ev_yield_pct = _calc_ev_dollars(true_win_prob, stake, share_price)
    _render_audit_results(true_win_prob, stake, share_price, ev_dollars, ev_yield_pct)


def render_hype_vs_reality() -> None:
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">📣 Sentiment vs. Math</h2>
            <p class="pq-tab-subtitle">
                Compare social sentiment to mathematical probability to detect narrative bubbles.
                <strong>When hype diverges from math by 20%+, a fading opportunity appears.</strong>
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Set the <em>Social Sentiment</em> slider to how bullish the
                public narrative feels (0 = very bearish, 100 = extremely bullish). Set <em>True Win</em>
                to your math-based probability. A big gap signals a potential edge.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            '<div class="pq-hype-col">',
            unsafe_allow_html=True,
        )
        st.markdown('<p class="pq-hype-label">📢 Social Sentiment</p>', unsafe_allow_html=True)
        sentiment = st.slider("Social Sentiment", 0.0, 100.0, 50.0, 0.5,
                              label_visibility="collapsed", key="hype_sent")
        sent_color = "#f59e0b" if sentiment > 65 else "#f43f5e" if sentiment < 35 else "#10b981"
        st.markdown(
            f'<p class="pq-hype-val" style="color:{sent_color};">{sentiment:.0f}%</p>'
            f'<p style="font-size:0.72rem;color:var(--text-muted);text-align:center;margin:0.2rem 0 0.5rem;">'
            f'{"Extremely bullish" if sentiment > 80 else "Bullish" if sentiment > 60 else "Neutral" if sentiment > 40 else "Bearish" if sentiment > 20 else "Extremely bearish"}</p>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)

    with col2:
        st.markdown('<div class="pq-hype-col">', unsafe_allow_html=True)
        st.markdown('<p class="pq-hype-label">📐 True Win Probability</p>', unsafe_allow_html=True)
        implied_prob = st.slider("True Win", 0.0, 100.0, 50.0, 0.5,
                                 label_visibility="collapsed", key="hype_real")
        math_color = "#10b981" if implied_prob > 65 else "#f43f5e" if implied_prob < 35 else "#5b7af5"
        st.markdown(
            f'<p class="pq-hype-val" style="color:{math_color};">{implied_prob:.0f}%</p>'
            f'<p style="font-size:0.72rem;color:var(--text-muted);text-align:center;margin:0.2rem 0 0.5rem;">'
            f'{"Strong favorite" if implied_prob > 70 else "Slight favorite" if implied_prob > 52 else "Toss-up" if implied_prob > 48 else "Slight underdog" if implied_prob > 30 else "Heavy underdog"}</p>',
            unsafe_allow_html=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)

    delta = sentiment - implied_prob
    abs_delta = abs(delta)
    delta_cls = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
    delta_sign = "+" if delta > 0 else ""

    st.markdown(
        f"""
        <div class="pq-sentiment-card">
            <div style="text-align:center;margin-bottom:0.75rem;">
                <div style="font-size:0.65rem;font-weight:800;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.1em;">Divergence Gap</div>
                <div class="pq-sentiment-delta {delta_cls}">{delta_sign}{delta:.0f}%</div>
                <div class="pq-sentiment-verdict">
                    {"Hype exceeds math — public overvaluing" if delta > 5 else "Math exceeds hype — market undervalued" if delta < -5 else "Sentiment aligned with probability"}
                </div>
            </div>
            <div class="pq-prob-bar-wrap">
                <div class="pq-prob-bar-label">
                    <span style="color:{"#f59e0b"};">Social Sentiment</span>
                    <span style="font-family:'JetBrains Mono',monospace;">{sentiment:.0f}%</span>
                </div>
                <div class="pq-prob-bar-track" style="height:8px;">
                    <div class="pq-prob-bar-fill" style="width:{int(sentiment)}%;--bar-color:#f59e0b;"></div>
                </div>
            </div>
            <div class="pq-prob-bar-wrap" style="margin-top:0.4rem;">
                <div class="pq-prob-bar-label">
                    <span style="color:{"#5b7af5"};">True Probability</span>
                    <span style="font-family:'JetBrains Mono',monospace;">{implied_prob:.0f}%</span>
                </div>
                <div class="pq-prob-bar-track" style="height:8px;">
                    <div class="pq-prob-bar-fill" style="width:{int(implied_prob)}%;--bar-color:#5b7af5;"></div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if delta >= DIVERGENCE_TRIGGER:
        st.markdown(
            f"""
            <div class="pq-bubble-badge">
                🔥 NARRATIVE BUBBLE DETECTED (+{delta:.0f}% divergence)<br>
                <span style="font-size:0.82rem;font-weight:600;opacity:0.85;">
                Public sentiment is {delta:.0f}% more bullish than the math supports.
                Consider fading the public — the NO side may offer strong value.
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.markdown(
            f"""
            <div class="pq-card" style="border-color:rgba(6,182,212,0.4);background:rgba(6,182,212,0.06);margin-top:1rem;text-align:center;">
                <div style="font-size:1.1rem;font-weight:800;color:var(--cyan);margin-bottom:0.4rem;">
                    📉 CROWD TOO BEARISH ({delta:.0f}% gap)
                </div>
                <div style="font-size:0.88rem;color:var(--text-secondary);">
                    Math suggests {abs(delta):.0f}% more probability than sentiment reflects.
                    The YES side may be cheap relative to true probability.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="pq-card" style="margin-top:1rem;text-align:center;">
                <div style="font-size:1rem;font-weight:700;color:var(--text-secondary);margin-bottom:0.3rem;">
                    ✓ Sentiment Aligned — No Narrative Edge
                </div>
                <div style="font-size:0.82rem;color:var(--text-muted);">
                    Gap is only {abs_delta:.0f}% — below the {DIVERGENCE_TRIGGER:.0f}% trigger threshold.
                    No actionable divergence detected.
                </div>
            </div>
            """,
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
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">🔍 Explore Markets</h2>
            <p class="pq-tab-subtitle">
                Browse all live prediction markets across Polymarket and Kalshi.
                Search by team, player, or event — then tap <strong>Select</strong> to load into the Arbs analyzer.
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Use the filters to narrow down markets by category or platform.
                Select a market row to push it directly to the Arbs tab for cross-book analysis.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
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

    st.markdown(
        f'<p class="pq-section-label">{len(filtered):,} markets · showing {start + 1}–{start + len(page_df)}</p>',
        unsafe_allow_html=True,
    )

    if st.session_state.get("explore_last_pick"):
        st.success(f"✓ Selected: {st.session_state.explore_last_pick}")

    _render_matchup_feed(page_df, odds_fmt)

    n1, n2, n3 = st.columns([1, 3, 1])
    with n1:
        if st.button("← Prev", key="explore_prev", disabled=page == 0, use_container_width=True):
            st.session_state.explore_page = page - 1
            st.rerun()
    with n2:
        st.markdown(
            f'<p class="pq-page-indicator" style="margin-top:0.5rem;">Page {page + 1} of {total_pages} &nbsp;·&nbsp; {len(filtered):,} total results</p>',
            unsafe_allow_html=True,
        )
    with n3:
        if st.button("Next →", key="explore_next", disabled=page >= total_pages - 1, use_container_width=True):
            st.session_state.explore_page = page + 1
            st.rerun()

    st.markdown('<p class="pq-section-label">Quick Actions</p>', unsafe_allow_html=True)
    qa1, qa2 = st.columns(2)
    with qa1:
        if st.button("⚖️ Audit This Bet", use_container_width=True):
            st.session_state.explore_action_hint = "Switch to the **⚖️ Check My Bet** tab to run the math on your pick."
            st.rerun()
    with qa2:
        if st.button("💰 Find Cross-Book Arb", use_container_width=True):
            st.session_state.explore_action_hint = (
                "Switch to the **💰 Risk-Free Arbs** tab — your selected market is pre-loaded."
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
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">💰 Risk-Free Arbs</h2>
            <p class="pq-tab-subtitle">
                Find cross-book arbitrage between Polymarket and Kalshi.
                <strong>When combined cost of YES + NO across both books is below 100¢, you lock guaranteed profit.</strong>
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Select a matching event on both Polymarket and Kalshi.
                The calculator automatically finds the best strategy (A or B) and shows exact
                contracts, cash needed, and guaranteed profit if an arb exists.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
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
        best_cost = min(cost_a, cost_b)
        gap_needed = (best_cost - 1.0) * 100
        st.markdown(
            f"""
            <div class="pq-card" style="border-color:rgba(244,63,94,0.3);background:var(--danger-dim);margin-top:0.75rem;">
                <div style="font-size:0.95rem;font-weight:800;color:var(--danger);margin-bottom:0.35rem;">
                    ⛔ No Risk-Free Lock Available
                </div>
                <div style="font-size:0.82rem;color:var(--text-secondary);line-height:1.5;">
                    Combined costs exceed 100¢ on both strategies.
                    Best pair costs <strong style="font-family:'JetBrains Mono',monospace;">{best_cost*100:.1f}¢</strong> —
                    needs to drop by <strong>{gap_needed:.1f}¢</strong> before a risk-free arb exists.
                    Monitor prices and refresh when lines move.
                </div>
            </div>
            """,
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
    st.markdown(
        """
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">📒 My Ledger</h2>
            <p class="pq-tab-subtitle">
                Your complete P&amp;L history synced live from Polymarket and Kalshi fills.
                <strong>Tracks daily performance, win/loss record, and capital at risk.</strong>
            </p>
            <div class="pq-how-to-box">
                <span>How to use:</span> Connect your API keys in the panel below, then tap
                <strong>Sync Fills</strong> to pull all your settled trades. The calendar shows
                daily net P&L at a glance — green = winning day, red = losing day.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    creds = _ledger_credentials()
    _render_api_keys_setup_panel(creds)

    if not creds["kalshi"] and not creds["polymarket"]:
        st.info("Connect at least one account above, then tap **Sync Fills** to populate your ledger.")

    if st.button("↻ Sync Fills", key="refresh_ledger", type="primary"):
        fetch_unified_ledger.clear()
        st.rerun()

    ledger = fetch_unified_ledger()
    daily_net, wl_record, capital_at_risk = _ledger_kpis(ledger)

    daily_color_cls = "profit" if daily_net >= 0 else "loss"
    daily_lbl = f"${daily_net:+,.2f}"

    st.markdown(
        f"""
        <div class="pq-kpi-row">
            <div class="pq-kpi-card">
                <div class="pq-kpi-label">Today's Net P&amp;L</div>
                <div class="pq-kpi-value {daily_color_cls}">{daily_lbl}</div>
            </div>
            <div class="pq-kpi-card">
                <div class="pq-kpi-label">Monthly W/L Record</div>
                <div class="pq-kpi-value neutral">{wl_record}</div>
            </div>
            <div class="pq-kpi-card">
                <div class="pq-kpi-label">Capital at Risk</div>
                <div class="pq-kpi-value neutral">${capital_at_risk:,.2f}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

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
    .block-container {
        padding-top: 0.75rem !important;
        padding-bottom: 1.5rem !important;
        max-width: 1200px !important;
    }
    header {visibility: hidden;}
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .stApp {
        background-color: #060a14 !important;
        color: #eaf0ff !important;
    }
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div {
        background-color: #080c1a !important;
    }
    </style>
    """, unsafe_allow_html=True)

_inject_global_css()
_init_session()


def _render_deploy_strip() -> None:
    """Always-visible deploy fingerprint so Cloud vs local is obvious."""
    st.markdown(
        f"""
        <div class="pq-deploy-strip">
            <span class="pq-deploy-live">● LIVE</span>
            <span>Build <span class="pq-build-tag">{html.escape(APP_BUILD)}</span></span>
            <span style="color:#2e3a5e;">·</span>
            <span>commit <code style="font-family:'JetBrains Mono',monospace;color:#5b7af5;font-size:0.9em;">{html.escape(GIT_SHA)}</code></span>
        </div>
        """,
        unsafe_allow_html=True,
    )


_render_deploy_strip()

st.markdown(
    f"""
    <div class="pq-topbar">
        <div class="pq-topbar-left">
            <div class="pq-topbar-logo">
                <span class="pq-topbar-brand">POLY-QUANT</span>
                <span class="pq-topbar-version">v{html.escape(APP_BUILD.split("-")[0])}</span>
            </div>
            <div class="pq-topbar-meta">
                <span class="pq-live-dot"></span>
                Polymarket &amp; Kalshi · Live market intelligence
            </div>
        </div>
        <div class="pq-topbar-meta" style="font-size:0.68rem;">
            Sports Betting · Prediction Markets · Arb Detection
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

tool_l, tool_r = st.columns([3, 1])
with tool_l:
    render_global_search_bar()
with tool_r:
    render_odds_format_toggle()


def _render_welcome_stats() -> None:
    """Top-of-page stat tiles giving a quick market health snapshot."""
    try:
        poly_df = fetch_polymarket_markets()
        poly_count = len(poly_df) if not poly_df.empty else 0
        vp_df = _filter_value_plays(poly_df) if not poly_df.empty else pd.DataFrame()
        vp_count = len(vp_df)
        top_edge = float(vp_df["Net EV Edge %"].max()) if not vp_df.empty else 0.0
        total_vol = float(poly_df["Volume"].sum()) if not poly_df.empty else 0.0
    except Exception:
        poly_count, vp_count, top_edge, total_vol = 0, 0, 0.0, 0.0

    vol_fmt = f"${total_vol/1_000_000:.1f}M" if total_vol >= 1_000_000 else f"${total_vol/1_000:.0f}K"
    edge_fmt = f"+{top_edge:.1f}%" if top_edge > 0 else "—"

    st.markdown(
        f"""
        <div class="pq-stats-strip">
            <div class="pq-stat-tile" style="--tile-accent:#5b7af5;">
                <div class="pq-stat-tile-label">Active Markets</div>
                <div class="pq-stat-tile-value">{poly_count:,}</div>
                <div class="pq-stat-tile-sub">Polymarket live contracts</div>
            </div>
            <div class="pq-stat-tile" style="--tile-accent:#10b981;">
                <div class="pq-stat-tile-label">Value Plays</div>
                <div class="pq-stat-tile-value">{vp_count}</div>
                <div class="pq-stat-tile-sub">Elite edges detected today</div>
            </div>
            <div class="pq-stat-tile" style="--tile-accent:#f59e0b;">
                <div class="pq-stat-tile-label">Top Edge</div>
                <div class="pq-stat-tile-value">{edge_fmt}</div>
                <div class="pq-stat-tile-sub">Best net EV available</div>
            </div>
            <div class="pq-stat-tile" style="--tile-accent:#06b6d4;">
                <div class="pq-stat-tile-label">Market Volume</div>
                <div class="pq-stat-tile-value">{vol_fmt}</div>
                <div class="pq-stat-tile-sub">Total 24h liquidity pool</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_onboard_guide() -> None:
    """Quick-start how-to strip for new users."""
    st.markdown(
        """
        <div class="pq-onboard-strip">
            <div class="pq-onboard-icon">💡</div>
            <div class="pq-onboard-text">
                <strong>How to use POLY-QUANT:</strong> This terminal finds mathematically
                profitable bets across Polymarket and Kalshi using quantitative edge analysis.
                <div class="pq-onboard-steps">
                    <span class="pq-onboard-step">1. Browse Value Plays for top edges</span>
                    <span class="pq-onboard-step">2. Explore all live markets</span>
                    <span class="pq-onboard-step">3. Check Bet audits your own picks</span>
                    <span class="pq-onboard-step">4. Arbs finds risk-free locked profits</span>
                    <span class="pq-onboard-step">5. Ledger tracks your P&amp;L</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    _render_welcome_stats()
    _render_onboard_guide()

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
            "🔍 Explore Markets",
            "⚖️ Check My Bet",
            "📣 Sentiment",
            "💰 Risk-Free Arbs",
            "📒 My Ledger",
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
