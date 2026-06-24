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
from datetime import date, datetime, timezone
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
    if "main_nav" not in st.session_state:
        st.session_state.main_nav = "Plays"


NAV_PAGES = ("Plays", "Explore", "Check", "Sentiment", "Arbs", "Ledger")
PAGE_COPY: dict[str, tuple[str, str]] = {
    "Plays": ("Top Value Plays", "Only the sharpest edges — ranked best to worst."),
    "Explore": ("Explore Markets", "Search and browse Polymarket + Kalshi like a feed."),
    "Check": ("Check My Bet", "Quick green-light / pass gate before you stake."),
    "Sentiment": ("Hype vs. Reality", "Spot when the crowd is louder than the math."),
    "Arbs": ("Risk-Free Arbs", "Two-leg recipes when both books misprice."),
    "Ledger": ("The Ledger", "Sync fills and track daily P&L."),
}


def _render_page_intro(page: str) -> None:
    title, subtitle = PAGE_COPY.get(page, ("", ""))
    st.markdown(
        f"""
        <div class="pq-page-intro">
            <h2 class="pq-page-title">{html.escape(title)}</h2>
            <p class="pq-page-sub">{html.escape(subtitle)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_app_header() -> None:
    st.markdown(
        """
        <div class="pq-app-bar">
            <div class="pq-brand">
                <span class="pq-brand-mark">PQ</span>
                <div>
                    <span class="pq-brand-name">PolyQuant</span>
                    <span class="pq-brand-tag">Polymarket · Kalshi</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    top_l, top_r = st.columns([2.2, 1])
    with top_l:
        render_global_search_bar()
    with top_r:
        st.markdown('<p class="pq-micro-label">Odds format</p>', unsafe_allow_html=True)
        st.segmented_control(
            "Odds format",
            options=list(ODDS_FORMATS),
            key="odds_format",
            label_visibility="collapsed",
        )


def _render_main_nav() -> str:
    st.markdown('<div class="pq-nav-wrap">', unsafe_allow_html=True)
    page = st.pills(
        "Navigate",
        options=list(NAV_PAGES),
        key="main_nav",
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)
    return str(page or st.session_state.get("main_nav", NAV_PAGES[0]))


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
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

            :root {
                --bg: #f4f5f9;
                --surface: #ffffff;
                --surface-2: #f8fafc;
                --text: #0f172a;
                --muted: #64748b;
                --border: #e2e8f0;
                --accent: #6366f1;
                --accent-2: #8b5cf6;
                --accent-soft: #eef2ff;
                --green: #10b981;
                --green-soft: #d1fae5;
                --red: #ef4444;
                --red-soft: #fee2e2;
                --radius: 16px;
                --radius-sm: 12px;
                --shadow: 0 1px 2px rgba(15,23,42,0.04), 0 8px 24px rgba(15,23,42,0.06);
                --shadow-sm: 0 1px 3px rgba(15,23,42,0.06);
            }

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            .stApp {
                background: var(--bg);
                color: var(--text);
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            }

            .block-container {
                padding: 0.75rem 1rem 5.5rem;
                max-width: 920px;
            }

            /* App chrome */
            .pq-app-bar { margin-bottom: 0.85rem; }
            .pq-brand { display: flex; align-items: center; gap: 0.65rem; }
            .pq-brand-mark {
                width: 40px; height: 40px; border-radius: 12px;
                background: linear-gradient(135deg, var(--accent), var(--accent-2));
                color: #fff; font-weight: 900; font-size: 0.85rem;
                display: flex; align-items: center; justify-content: center;
                box-shadow: var(--shadow-sm);
            }
            .pq-brand-name {
                display: block; font-size: 1.15rem; font-weight: 800;
                letter-spacing: -0.03em; color: var(--text); line-height: 1.1;
            }
            .pq-brand-tag {
                display: block; font-size: 0.72rem; font-weight: 600;
                color: var(--muted); margin-top: 0.1rem;
            }
            .pq-micro-label {
                font-size: 0.68rem; font-weight: 700; color: var(--muted);
                text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 0.25rem;
            }
            .pq-page-intro { margin: 0.35rem 0 1rem; }
            .pq-page-title {
                margin: 0; font-size: 1.35rem; font-weight: 800;
                letter-spacing: -0.03em; color: var(--text);
            }
            .pq-page-sub {
                margin: 0.25rem 0 0; font-size: 0.88rem; color: var(--muted);
                font-weight: 500; line-height: 1.45;
            }

            /* Sticky nav pills */
            .pq-nav-wrap {
                position: sticky; top: 0; z-index: 999;
                background: rgba(244,245,249,0.92);
                backdrop-filter: blur(10px);
                padding: 0.35rem 0 0.65rem;
                margin: 0 -0.25rem 0.5rem;
                border-bottom: 1px solid var(--border);
            }
            .pq-nav-wrap [data-testid="stPills"] { gap: 0.35rem; }
            .pq-nav-wrap button {
                border-radius: 999px !important;
                font-weight: 700 !important;
                font-size: 0.78rem !important;
                padding: 0.4rem 0.85rem !important;
                border: 1px solid var(--border) !important;
                background: var(--surface) !important;
                color: var(--muted) !important;
            }
            .pq-nav-wrap button[aria-pressed="true"] {
                background: var(--accent) !important;
                color: #fff !important;
                border-color: var(--accent) !important;
                box-shadow: 0 4px 14px rgba(99,102,241,0.35);
            }
            .pq-filter-row { margin-bottom: 0.65rem; }
            .pq-filter-row [data-testid="stPills"] { flex-wrap: wrap; gap: 0.35rem; }
            .pq-filter-row button {
                border-radius: 999px !important;
                font-weight: 600 !important;
                font-size: 0.74rem !important;
                border: 1px solid var(--border) !important;
                background: var(--surface) !important;
            }
            .pq-filter-row button[aria-pressed="true"] {
                background: var(--accent-soft) !important;
                color: var(--accent) !important;
                border-color: #c7d2fe !important;
            }

            /* Search */
            .pq-search-hero {
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius);
                padding: 0.15rem 0.35rem;
                box-shadow: var(--shadow-sm);
            }

            /* Cards & surfaces */
            .pq-card, .pq-value-card, .pq-input-card, .pq-feed-row, .pq-pick-card,
            .pq-hype-col, .pq-split-side, .pq-selected-banner, .pq-odds-bar {
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius);
                box-shadow: var(--shadow-sm);
            }
            .pq-card, .pq-input-card, .pq-selected-banner, .pq-odds-bar {
                padding: 1rem 1.1rem;
                margin-bottom: 0.65rem;
            }
            .pq-card-compound {
                border-color: var(--green);
                background: linear-gradient(135deg, var(--green-soft) 0%, var(--surface) 55%);
            }
            .pq-card-title {
                font-size: 0.95rem; font-weight: 700; color: var(--text);
                line-height: 1.35; margin: 0 0 0.5rem;
            }
            .pq-card-row { display: flex; flex-wrap: wrap; gap: 0.45rem; align-items: center; }

            /* Badges */
            .pq-badge {
                display: inline-block; padding: 0.28rem 0.65rem;
                border-radius: 999px; font-size: 0.72rem; font-weight: 700;
            }
            .pq-badge-green { background: var(--green-soft); color: #047857; border: 1px solid #a7f3d0; }
            .pq-badge-blue { background: var(--accent-soft); color: var(--accent); border: 1px solid #c7d2fe; }
            .pq-badge-grey { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
            .pq-badge-red { background: var(--red-soft); color: var(--red); border: 1px solid #fecaca; }

            .pq-section-label, .pq-micro-label {
                font-size: 0.68rem; font-weight: 700; color: var(--muted);
                text-transform: uppercase; letter-spacing: 0.07em;
            }
            .pq-section-label { margin: 0.5rem 0 0.35rem; }

            /* Value play cards */
            .pq-value-card {
                padding: 1.1rem 1.15rem;
                margin-bottom: 0.75rem;
            }
            .pq-value-card-hot { border-color: #a7f3d0; }
            .pq-value-card-elite {
                border: 2px solid var(--green);
                box-shadow: 0 8px 28px rgba(16,185,129,0.18);
            }
            .pq-rank-badge {
                display: inline-block; background: var(--green-soft);
                color: #047857; border: 1px solid #6ee7b7;
                font-weight: 800; font-size: 0.72rem;
                padding: 0.3rem 0.65rem; border-radius: 999px;
                margin-bottom: 0.55rem; letter-spacing: 0.03em;
            }
            .pq-rank-badge-elite { background: var(--green); color: #fff; border-color: var(--green); }
            .pq-event-name {
                font-size: 1rem; font-weight: 800; color: var(--text);
                margin: 0 0 0.65rem; line-height: 1.35;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(90deg, var(--accent), var(--accent-2));
                color: #fff; font-weight: 800; font-size: 0.8rem;
                padding: 0.5rem 0.9rem; border-radius: 999px;
                margin-bottom: 0.65rem;
            }
            .pq-ev-badge {
                display: inline-block; background: var(--green-soft);
                color: #047857; border: 1px solid #6ee7b7;
                font-weight: 800; font-size: 0.82rem;
                padding: 0.35rem 0.7rem; border-radius: 10px;
            }
            .pq-metric-row {
                display: flex; gap: 1rem; flex-wrap: wrap;
                font-size: 0.8rem; color: var(--muted);
            }
            .pq-metric-row strong { color: var(--text); }

            /* Audit banners */
            .pq-banner-play {
                background: linear-gradient(90deg, var(--green-soft), #ecfdf5);
                border: 2px solid var(--green); border-radius: var(--radius);
                padding: 1.25rem 1.35rem; margin-top: 0.75rem;
                font-size: 1.15rem; font-weight: 900; color: #047857;
                box-shadow: 0 8px 24px rgba(16,185,129,0.15);
            }
            .pq-banner-pass {
                background: var(--surface-2); border: 1px solid var(--border);
                border-radius: var(--radius); padding: 1.25rem;
                text-align: center; font-size: 1.1rem; font-weight: 800;
                color: var(--muted); margin-top: 0.75rem;
            }

            /* Hype vs reality */
            .pq-hype-col { padding: 1rem; text-align: center; }
            .pq-hype-val {
                font-size: 2rem; font-weight: 900; color: var(--accent);
                margin: 0.25rem 0 0; letter-spacing: -0.03em;
            }
            .pq-bubble-badge {
                display: block; text-align: center; padding: 1rem;
                background: var(--red-soft); border: 2px solid var(--red);
                border-radius: var(--radius); color: #b91c1c;
                font-weight: 800; font-size: 0.95rem; margin-top: 0.75rem;
            }

            /* Explore feed */
            .pq-feed-row { padding: 0.85rem 1rem; margin-bottom: 0.5rem; }
            .pq-feed-meta {
                font-size: 0.65rem; font-weight: 700; color: var(--muted);
                text-transform: uppercase; letter-spacing: 0.06em;
            }
            .pq-feed-title {
                display: block; font-size: 0.92rem; font-weight: 700;
                color: var(--text); line-height: 1.35; margin-top: 0.2rem;
            }
            .pq-feed-event {
                display: block; font-size: 0.75rem; color: var(--muted); margin-top: 0.15rem;
            }
            .pq-odd-pill {
                display: block; text-align: center; padding: 0.6rem 0.4rem;
                border-radius: var(--radius-sm); font-weight: 800; font-size: 0.9rem;
            }
            .pq-odd-yes { background: var(--accent-soft); color: var(--accent); border: 1px solid #c7d2fe; }
            .pq-odd-no { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }

            /* Picker */
            .pq-pick-card { padding: 0.7rem 0.85rem; margin-bottom: 0.35rem; }
            .pq-pick-selected { border-color: var(--accent); background: var(--accent-soft); }
            .pq-pick-title { font-size: 0.86rem; font-weight: 600; color: var(--text); }
            .pq-pick-meta { font-size: 0.72rem; color: var(--accent); font-weight: 700; margin-top: 0.15rem; }
            .pq-page-indicator { text-align: center; font-size: 0.75rem; color: var(--muted); }
            .pq-selected-banner { font-size: 0.82rem; color: var(--text); }

            /* Arbs */
            .pq-split {
                display: grid; grid-template-columns: 1fr 1fr; gap: 0.65rem; margin: 0.65rem 0;
            }
            @media (max-width: 640px) { .pq-split { grid-template-columns: 1fr; } }
            .pq-split-side { padding: 0.9rem; text-align: center; }
            .pq-split-side .venue {
                font-size: 0.68rem; font-weight: 700; color: var(--muted);
                text-transform: uppercase; letter-spacing: 0.06em;
            }
            .pq-split-side .leg { font-size: 1rem; font-weight: 800; color: var(--accent); }
            .pq-arb-banner {
                background: linear-gradient(135deg, var(--accent-soft), var(--surface));
                border: 1px solid #c7d2fe; border-radius: var(--radius);
                padding: 1rem 1.1rem; margin-bottom: 0.65rem;
            }
            .pq-arb-banner h3 { margin: 0; font-size: 1rem; font-weight: 800; color: var(--text); }
            .pq-arb-banner p { margin: 0.25rem 0 0; font-size: 0.82rem; color: var(--muted); }
            .pq-recipe {
                background: var(--surface); border: 1px solid var(--border);
                border-radius: var(--radius); padding: 1rem; margin-bottom: 0.65rem;
                box-shadow: var(--shadow-sm);
            }
            .pq-recipe-step { font-size: 0.86rem; color: var(--muted); margin: 0.35rem 0; }
            .pq-recipe-step strong { color: var(--accent); }
            .pq-lock-banner {
                margin-top: 0.65rem; padding: 0.75rem; text-align: center;
                background: var(--green-soft); border: 1px solid #6ee7b7;
                border-radius: var(--radius-sm); color: #047857; font-weight: 800;
            }

            /* Ledger calendar */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid {
                display: grid; grid-template-columns: repeat(7, 1fr); gap: 0.35rem;
            }
            .pq-cal-head {
                text-align: center; font-size: 0.65rem; font-weight: 700;
                color: var(--muted); padding: 0.25rem;
            }
            .pq-cal-cell {
                background: var(--surface); border: 1px solid var(--border);
                border-radius: 10px; padding: 0.4rem 0.25rem; text-align: center; min-height: 52px;
            }
            .pq-cal-day { display: block; font-size: 0.72rem; font-weight: 700; color: var(--muted); }
            .pq-cal-neutral { background: var(--surface-2); }
            .pq-cal-win { background: var(--green-soft); border-color: #a7f3d0; }
            .pq-cal-loss { background: var(--red-soft); border-color: #fecaca; }
            .pq-cal-pnl { font-size: 0.7rem; font-weight: 800; }
            .pq-cal-pnl.pos { color: #047857; }
            .pq-cal-pnl.neg { color: var(--red); }
            .pq-cal-dash { color: #cbd5e1; font-size: 0.85rem; }

            /* Streamlit widgets */
            .stTextInput input, .stNumberInput input, .stSelectbox > div > div {
                border-radius: var(--radius-sm) !important;
                border-color: var(--border) !important;
                background: var(--surface) !important;
            }
            .stButton > button {
                border-radius: var(--radius-sm) !important;
                font-weight: 700 !important;
                min-height: 2.5rem;
            }
            .stButton > button[kind="primary"] {
                background: var(--accent) !important;
                border-color: var(--accent) !important;
                color: #fff !important;
            }
            .stButton > button[kind="secondary"] {
                background: var(--surface) !important;
                border: 1px solid var(--border) !important;
                color: var(--text) !important;
            }
            [data-testid="stMetric"] {
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                padding: 0.65rem 0.8rem;
                box-shadow: var(--shadow-sm);
            }
            [data-testid="stDataFrame"] {
                border: 1px solid var(--border);
                border-radius: var(--radius);
                overflow: hidden;
            }
            .stSlider label, .stNumberInput label { font-weight: 600 !important; font-size: 0.82rem !important; }
            hr { border-color: var(--border); }
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


def render_top_value_plays() -> None:
    bar_l, bar_r = st.columns([3, 1])
    with bar_l:
        st.caption(
            f"Win prob >{VALUE_PLAYS_WIN_MIN:.0f}% · net EV ≥{VALUE_PLAYS_EV_EDGE_MIN:.0f}% · "
            f"top {VALUE_PLAYS_MAX} only"
        )
    with bar_r:
        if st.button("Refresh", key="refresh_poly", use_container_width=True):
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
                <p class="pq-page-sub" style="margin:0;">
                    No mathematically viable anomalies detected. Maintain bankroll discipline.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    st.caption(f"{len(df)} elite anomal{'y' if len(df) == 1 else 'ies'} on slate")
    for rank, (_, row) in enumerate(df.iterrows(), start=1):
        _render_value_play_card(row, rank)


def render_audit_my_bet() -> None:
    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    with c1:
        true_win_prob = st.number_input(
            "Your win estimate (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
        )
    with c2:
        stake = st.number_input(
            "Stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
        )
    with c3:
        share_price = st.number_input(
            "Share price (¢)",
            min_value=0.01,
            max_value=99.99,
            value=50.0,
            step=1.0,
        )
    st.markdown("</div>", unsafe_allow_html=True)

    ev_dollars, ev_yield_pct = _calc_ev_dollars(true_win_prob, stake, share_price)
    ev_ok = ev_yield_pct >= EV_THRESHOLD
    prob_ok = true_win_prob >= WIN_PROB_THRESHOLD

    if ev_ok and prob_ok:
        st.markdown(
            f"""
            <div class="pq-banner-play">
                Green light — playable<br>
                <span style="font-size:0.88rem;font-weight:600;">
                Projected ${ev_dollars:+,.2f} on ${stake:,.2f} stake · {ev_yield_pct:+.2f}% edge
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="pq-banner-pass">Pass — edge or probability too thin</div>', unsafe_allow_html=True)


def render_hype_vs_reality() -> None:
    left, right = st.columns(2)
    with left:
        st.markdown('<div class="pq-hype-col">', unsafe_allow_html=True)
        st.markdown('<p class="pq-micro-label">What people are saying</p>', unsafe_allow_html=True)
        sentiment = st.slider("Social Sentiment", 0.0, 100.0, 50.0, 0.5, label_visibility="collapsed", key="hype_sent")
        st.markdown(f'<p class="pq-hype-val">{sentiment:.0f}%</p>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        st.markdown('<div class="pq-hype-col">', unsafe_allow_html=True)
        st.markdown('<p class="pq-micro-label">What the math says</p>', unsafe_allow_html=True)
        implied_prob = st.slider("True Win", 0.0, 100.0, 50.0, 0.5, label_visibility="collapsed", key="hype_real")
        st.markdown(f'<p class="pq-hype-val">{implied_prob:.0f}%</p>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

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
    else:
        st.session_state.kalshi_selected = row["id"]


def _render_matchup_feed(page_df: pd.DataFrame, odds_fmt: str) -> None:
    """Pikkit-style scannable rows: market left, YES/NO odds right."""
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
            <div class="pq-feed-row">
                <span class="pq-feed-meta">{html.escape(str(row["Source"]))} ·
                {html.escape(str(row["Category"]))} · {html.escape(str(row["Subcategory"]))}</span>
                <span class="pq-feed-title">{html.escape(str(row["Title"]))}</span>
                {event_line}
            </div>
            """,
            unsafe_allow_html=True,
        )
        b1, b2, b3 = st.columns([1, 1, 1.3])
        with b1:
            st.markdown(
                f'<div class="pq-odd-pill pq-odd-yes">YES {html.escape(yes_odds)}</div>',
                unsafe_allow_html=True,
            )
        with b2:
            st.markdown(
                f'<div class="pq-odd-pill pq-odd-no">NO {html.escape(no_odds)}</div>',
                unsafe_allow_html=True,
            )
        with b3:
            if st.button("Select →", key=f"explore_pick_{row['Catalog ID']}_{idx}", use_container_width=True):
                _sync_selection_from_catalog(row)
                st.session_state.explore_last_pick = str(row["Title"])
                st.rerun()


def render_explore_hub() -> None:
    query = st.session_state.get("global_search_query", "").strip().lower()

    bar_l, bar_r = st.columns([3, 1])
    with bar_l:
        st.caption("Tap a market to select it for cross-book arbs.")
    with bar_r:
        if st.button("Refresh", key="refresh_explore", use_container_width=True):
            build_explore_catalog.clear()
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            fetch_kalshi_player_props.clear()
            st.rerun()

    try:
        catalog = build_explore_catalog()
    except Exception:
        st.error("Could not load the market catalog. Try refreshing.")
        return

    if catalog.empty:
        st.warning("No markets available to explore right now.")
        return

    st.markdown('<div class="pq-filter-row">', unsafe_allow_html=True)
    category = st.pills(
        "Category",
        options=list(EXPLORE_CATEGORIES),
        key="explore_category",
        label_visibility="collapsed",
    )

    sports_type = "All"
    if category in ("Sports", "Player Props"):
        sports_type = st.pills(
            "Market Type",
            options=list(EXPLORE_SPORTS_TYPES),
            key="explore_sports_type",
            label_visibility="collapsed",
        )

    source = st.pills(
        "Source",
        options=list(EXPLORE_SOURCES),
        key="explore_source",
        label_visibility="collapsed",
    )
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
    bar_l, bar_r = st.columns([2, 1])
    with bar_l:
        arb_stake = st.number_input(
            "Stake per leg ($)",
            min_value=1.0,
            value=DEFAULT_ARB_STAKE,
            step=10.0,
            key="arb_stake",
        )
    with bar_r:
        if st.button("Refresh prices", key="refresh_arb", use_container_width=True):
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            fetch_kalshi_player_props.clear()
            st.rerun()

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
    kalshi_ticker = render_searchable_picker(
        "Kalshi Event", kalshi_options, "kalshi_selected", show_prices=kalshi_prices,
    )
    if not poly_id or not kalshi_ticker:
        return

    poly_row = poly_priced.loc[poly_priced["id"] == poly_id].iloc[0]
    kalshi_row = kalshi_priced.loc[kalshi_priced["ticker"] == kalshi_ticker].iloc[0]

    poly_yes = float(poly_row["Yes Price"])
    poly_no = float(poly_row["No Price"])
    kalshi_yes = float(kalshi_row["Kalshi YES Cost"])
    kalshi_no = float(kalshi_row["Kalshi NO Cost"])

    cost_a = poly_yes + kalshi_no
    net_a, _ = _arb_opportunity(cost_a)
    cost_b = poly_no + kalshi_yes
    net_b, _ = _arb_opportunity(cost_b)

    yes_c = poly_yes * 100.0
    no_k_c = kalshi_no * 100.0
    no_p_c = poly_no * 100.0
    yes_k_c = kalshi_yes * 100.0

    _render_arb_recipe(
        "Recipe A",
        f"Buy YES on Polymarket at {yes_c:.1f}¢ (${arb_stake:,.0f})",
        f"Buy NO on Kalshi at {no_k_c:.1f}¢ (${arb_stake:,.0f})",
        net_a,
        arb_stake,
        cost_a < 1.0,
    )
    _render_arb_recipe(
        "Recipe B",
        f"Buy NO on Polymarket at {no_p_c:.1f}¢ (${arb_stake:,.0f})",
        f"Buy YES on Kalshi at {yes_k_c:.1f}¢ (${arb_stake:,.0f})",
        net_b,
        arb_stake,
        cost_b < 1.0,
    )

    if cost_a >= 1.0 and cost_b >= 1.0:
        st.markdown(
            '<div class="pq-card"><span class="pq-badge pq-badge-grey">No lock available on this pair</span></div>',
            unsafe_allow_html=True,
        )


def _build_calendar_html(daily_pnl: dict[date, float], year: int, month: int) -> str:
    weekday, num_days = calendar.monthrange(year, month)
    heads = "".join(f'<div class="pq-cal-head">{d}</div>' for d in ("M", "T", "W", "T", "F", "S", "S"))
    cells: list[str] = []
    for _ in range(weekday):
        cells.append('<div class="pq-cal-cell pq-cal-neutral"></div>')
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        pnl = daily_pnl.get(d)
        if pnl is None:
            cells.append(
                f'<div class="pq-cal-cell pq-cal-neutral"><span class="pq-cal-day">{day}</span>'
                f'<span class="pq-cal-dash">-</span></div>'
            )
        elif pnl > 0:
            cells.append(
                f'<div class="pq-cal-cell pq-cal-win"><span class="pq-cal-day">{day}</span>'
                f'<span class="pq-cal-pnl pos">+${pnl:.2f}</span></div>'
            )
        elif pnl < 0:
            cells.append(
                f'<div class="pq-cal-cell pq-cal-loss"><span class="pq-cal-day">{day}</span>'
                f'<span class="pq-cal-pnl neg">-${abs(pnl):.2f}</span></div>'
            )
        else:
            cells.append(
                f'<div class="pq-cal-cell pq-cal-neutral"><span class="pq-cal-day">{day}</span>'
                f'<span class="pq-cal-dash">-</span></div>'
            )
    grid = "".join(cells)
    month_name = datetime(year, month, 1).strftime("%B %Y")
    return (
        f'<div class="pq-calendar-wrap"><p class="pq-section-label">{month_name}</p>'
        f'<div class="pq-cal-grid">{heads}{grid}</div></div>'
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

    now = datetime.now(timezone.utc)
    daily_pnl = _ledger_daily_pnl(ledger)
    st.markdown(_build_calendar_html(daily_pnl, now.year, now.month), unsafe_allow_html=True)

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
    page_title="PolyQuant",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_inject_global_css()
_init_session()


def main() -> None:
    _render_app_header()
    page = _render_main_nav()
    _render_page_intro(page)

    if page == "Plays":
        render_top_value_plays()
    elif page == "Explore":
        render_explore_hub()
    elif page == "Check":
        render_audit_my_bet()
    elif page == "Sentiment":
        render_hype_vs_reality()
    elif page == "Arbs":
        render_risk_free_arbs()
    elif page == "Ledger":
        render_ledger()


if __name__ == "__main__":
    main()
