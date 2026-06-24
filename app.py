"""
POLY-QUANT-v1
=============
Executive sports-betting intelligence terminal.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import html
import json
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st

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

        rows.append(
            {
                "id": str(market.get("id") or market.get("conditionId") or question),
                "Question": question,
                "Yes Price": yes_price,
                "No Price": no_price,
                "Volume": volume if volume is not None else 0.0,
                "Liquidity": liquidity if liquidity is not None else 0.0,
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

        yes_ask = parse_dollar_string(market.get("yes_ask_dollars"))
        yes_bid = parse_dollar_string(market.get("yes_bid_dollars"))

        kalshi_yes_cost = yes_ask
        kalshi_no_cost = (1.0 - yes_bid) if yes_bid is not None else None

        title = market.get("title") or market.get("ticker") or "—"
        ticker = market.get("ticker") or title

        rows.append(
            {
                "ticker": ticker,
                "Title": title,
                "Yes Ask": kalshi_yes_cost,
                "Yes Bid": yes_bid,
                "Kalshi YES Cost": kalshi_yes_cost,
                "Kalshi NO Cost": kalshi_no_cost,
            }
        )

    return pd.DataFrame(rows)


def _filter_value_plays(raw_df: pd.DataFrame) -> pd.DataFrame:
    """Backend liquidity + pricing filter (unchanged)."""
    df = raw_df.dropna(subset=["No Price"]).copy()
    df = df[df["Volume"] >= MIN_VOLUME].copy()
    return df.sort_values("No Price", ascending=True).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Presentation layer — plain English, visual hierarchy only
# --------------------------------------------------------------------------- #

ODDS_FORMATS = ("Cents", "Percentage", "American")
PICKER_PAGE_SIZE = 6


def _init_session() -> None:
    if "odds_format" not in st.session_state:
        st.session_state.odds_format = "American"
    if "poly_selected" not in st.session_state:
        st.session_state.poly_selected = None
    if "kalshi_selected" not in st.session_state:
        st.session_state.kalshi_selected = None


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


def render_odds_format_toggle() -> None:
    st.markdown(
        '<p class="pq-section-label">Odds Display</p>',
        unsafe_allow_html=True,
    )
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
    query = st.text_input(
        f"Search {label}",
        key=f"{session_key}_search",
        placeholder="Search by keyword…",
        label_visibility="collapsed",
    ).lower().strip()

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

            /* Header */
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
        </style>
        """,
        unsafe_allow_html=True,
    )


def _build_display_grid(df: pd.DataFrame, odds_fmt: str) -> pd.DataFrame:
    """Map backend rows → plain-English scannable columns (presentation only)."""
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        no_p = float(r["No Price"])
        in_zone = _in_strike_zone(no_p)
        model_win = DISPLAY_MODEL_WIN_PCT if in_zone else no_p * 100.0
        _, ev_edge = _calc_ev_dollars(model_win, 100.0, no_p * 100.0)

        if in_zone:
            play = "🟢 Easy Money Compounding Play — BUY NO"
        else:
            play = f"BUY NO @ {format_odds_display(no_p, odds_fmt)}"

        rows.append(
            {
                "Matchup / Market": r["Question"],
                "The Recommended Play": play,
                "Implied Odds": format_odds_display(no_p, odds_fmt),
                "Our Model Win Chance %": round(model_win, 1),
                "EV Edge %": round(ev_edge, 2),
                "_in_zone": in_zone,
            }
        )
    return pd.DataFrame(rows)


def _highlight_compound_plays(row: pd.Series) -> list[str]:
    style = (
        "background-color: rgba(63, 185, 80, 0.14); border-left: 3px solid #3fb950;"
        if row.get("_in_zone")
        else ""
    )
    return [style] * len(row)


def _render_compound_cards(df: pd.DataFrame, odds_fmt: str) -> None:
    compound = df[df["No Price"].between(STRIKE_LO, STRIKE_HI)]
    if compound.empty:
        return

    st.markdown("#### ⭐ Flagged Compounding Plays")
    for _, r in compound.head(6).iterrows():
        no_p = float(r["No Price"])
        _, ev_edge = _calc_ev_dollars(DISPLAY_MODEL_WIN_PCT, 100.0, no_p * 100.0)
        q = html.escape(str(r["Question"]))
        st.markdown(
            f"""
            <div class="pq-card pq-card-compound">
                <p class="pq-card-title">{q}</p>
                <div class="pq-card-row">
                    <span class="pq-badge pq-badge-green">Easy Money Compounding Play</span>
                    <span class="pq-badge pq-badge-blue">NO {format_odds_display(no_p, odds_fmt)}</span>
                    <span class="pq-stat">Odds <strong>{format_odds_display(no_p, odds_fmt)}</strong></span>
                    <span class="pq-stat">Model <strong>{DISPLAY_MODEL_WIN_PCT:.1f}%</strong></span>
                    <span class="pq-stat">Edge <strong>{ev_edge:+.2f}%</strong></span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_top_value_plays() -> None:
    st.markdown("### 🔥 Top Value Plays")
    st.markdown(
        '<p style="color:#8b949e;font-size:0.82rem;margin-top:-0.5rem;">'
        "Live markets ranked by best NO prices. Green highlights = compounding opportunities."
        "</p>",
        unsafe_allow_html=True,
    )

    if st.button("↻ Refresh Markets", key="refresh_poly", use_container_width=False):
        fetch_polymarket_markets.clear()
        st.rerun()

    try:
        raw_df = fetch_polymarket_markets()
    except requests.exceptions.RequestException:
        st.error("Unable to load market data right now. Try refreshing in a moment.")
        return
    except (ValueError, json.JSONDecodeError):
        st.error("Market data came back in an unexpected format. Try again shortly.")
        return
    except Exception:
        st.error("Something went wrong loading markets. Please refresh.")
        return

    if raw_df.empty:
        st.warning("No active markets found.")
        return

    df = _filter_value_plays(raw_df)
    if df.empty:
        st.warning("No qualifying plays right now. Check back when more volume hits the board.")
        return

    market_search = st.text_input(
        "Filter markets",
        key="value_plays_search",
        placeholder="Search by keyword…",
        label_visibility="collapsed",
    )
    if market_search.strip():
        df = df[df["Question"].str.contains(market_search.strip(), case=False, na=False)].copy()
        if df.empty:
            st.info("No markets match your search.")
            return

    compound_count = int(df["No Price"].between(STRIKE_LO, STRIKE_HI).sum())
    odds_fmt = get_odds_format()

    k1, k2, k3 = st.columns(3)
    k1.metric("Live Markets", f"{len(df)}")
    k2.metric("Compounding Plays", f"{compound_count}")
    k3.metric("Best NO Price", format_odds_display(float(df["No Price"].iloc[0]), odds_fmt))

    _render_compound_cards(df, odds_fmt)

    display_df = _build_display_grid(df, odds_fmt)
    show_cols = [
        "Matchup / Market",
        "The Recommended Play",
        "Implied Odds",
        "Our Model Win Chance %",
        "EV Edge %",
    ]
    styled = (
        display_df[show_cols + ["_in_zone"]]
        .style.apply(_highlight_compound_plays, axis=1)
        .hide(subset=["_in_zone"], axis="columns")
        .format({"Our Model Win Chance %": "{:.1f}%", "EV Edge %": "{:+.2f}%"})
    )

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        height=min(520, 48 + len(display_df) * 38),
        column_config={
            "Matchup / Market": st.column_config.TextColumn(
                "Matchup / Market", width=COL_QUESTION
            ),
            "The Recommended Play": st.column_config.TextColumn(
                "The Recommended Play", width=180
            ),
            "Implied Odds": st.column_config.TextColumn("Implied Odds", width=COL_PRICE),
            "Our Model Win Chance %": st.column_config.NumberColumn(
                "Our Model Win Chance %", width=COL_PRICE, format="%.1f%%"
            ),
            "EV Edge %": st.column_config.NumberColumn(
                "EV Edge %", width=COL_PRICE, format="%+.2f%%"
            ),
        },
    )


def render_audit_my_bet() -> None:
    st.markdown("### ⚖️ Audit My Bet")
    st.markdown(
        '<p style="color:#8b949e;font-size:0.82rem;margin-top:-0.5rem;">'
        "Plug in your numbers — we'll tell you if the bet is worth taking."
        "</p>",
        unsafe_allow_html=True,
    )

    st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)

    r1, r2 = st.columns(2)
    with r1:
        true_win_prob = st.number_input(
            "Our Model's Win Probability (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
        )
    with r2:
        stake = st.number_input(
            "Your Planned Stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
        )

    st.markdown('<p class="pq-section-label">Offered Odds Format</p>', unsafe_allow_html=True)
    offered_fmt = st.segmented_control(
        "Offered Odds Format",
        options=list(ODDS_FORMATS),
        default="Cents",
        key="audit_odds_fmt",
        label_visibility="collapsed",
    )

    if offered_fmt == "American":
        odds_raw = st.text_input(
            "Offered Bookmaker Odds",
            value="+150",
            placeholder="e.g. +150, -223, 150",
            help="Enter American odds with or without the + sign.",
        )
        share_price = parse_offered_odds(odds_raw, "american")
        if share_price is None:
            st.error("Enter valid American odds (e.g. +150 or -223).")
            st.markdown("</div>", unsafe_allow_html=True)
            return
    else:
        default_val = 50.0
        label = "Offered Price (¢)" if offered_fmt == "Cents" else "Implied Win Probability (%)"
        odds_raw = st.number_input(
            label,
            min_value=0.01,
            max_value=99.99,
            value=default_val,
            step=0.5,
        )
        share_price = parse_offered_odds(odds_raw, offered_fmt.lower())
        if share_price is None:
            st.error("Enter a valid price between 0 and 100.")
            st.markdown("</div>", unsafe_allow_html=True)
            return

    st.markdown("</div>", unsafe_allow_html=True)

    ev_dollars, ev_yield_pct = _calc_ev_dollars(true_win_prob, stake, share_price)
    ev_ok = ev_yield_pct >= EV_THRESHOLD
    prob_ok = true_win_prob >= WIN_PROB_THRESHOLD
    odds_display = format_odds_display(share_price / 100.0, offered_fmt.lower())

    if ev_ok and prob_ok:
        st.markdown(
            f"""
            <div class="pq-verdict-play">
                <h2>🟢 GREEN LIGHT — PLAYABLE EDGE</h2>
                <p>Projected return on a <strong>${stake:,.2f}</strong> stake:
                <strong>${ev_dollars:+,.2f}</strong> expected value
                (<strong>{ev_yield_pct:+.2f}%</strong> edge).</p>
                <p style="margin-top:0.5rem;">Plant your limit order at
                <strong>{odds_display}</strong>.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        reasons: list[str] = []
        if not ev_ok:
            reasons.append(f"Edge is only {ev_yield_pct:+.2f}% — need at least {EV_THRESHOLD:.1f}%.")
        if not prob_ok:
            reasons.append(
                f"Win probability is {true_win_prob:.1f}% — need at least {WIN_PROB_THRESHOLD:.0f}%."
            )
        reason_html = " ".join(reasons)
        st.markdown(
            f"""
            <div class="pq-verdict-pass">
                <h2>🔴 HARD PASS — NEGATIVE ROI TRAP</h2>
                <p>{html.escape(reason_html)}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_trap_detector() -> None:
    st.markdown("### 🚨 Trap Detector")
    st.markdown(
        '<p style="color:#8b949e;font-size:0.82rem;margin-top:-0.5rem;">'
        "Compare what the crowd thinks vs. what the math says."
        "</p>",
        unsafe_allow_html=True,
    )

    s1, s2 = st.columns(2)
    with s1:
        sentiment = st.slider(
            "Public Forum Hype (Reddit / Socials)",
            0.0, 100.0, 50.0, 0.5,
        )
    with s2:
        implied_prob = st.slider(
            "Actual Mathematical Probability",
            0.0, 100.0, 50.0, 0.5,
        )

    delta = sentiment - implied_prob

    d1, d2, d3 = st.columns(3)
    d1.metric("Public Hype", f"{sentiment:.0f}")
    d2.metric("Real Probability", f"{implied_prob:.0f}%")
    d3.metric("Gap", f"{delta:+.0f} pts")

    if delta >= DIVERGENCE_TRIGGER:
        st.markdown(
            """
            <div class="pq-trap-banner">
                <h3>⚠️ NARRATIVE BUBBLE DETECTED</h3>
                <p>The public is wildly overvaluing this outcome. Fade the noise and
                look for Asymmetric NO value.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.markdown(
            """
            <div class="pq-card" style="border-color:#58a6ff;">
                <span class="pq-badge pq-badge-blue">Undervalued YES Opportunity</span>
                <p style="margin:0.5rem 0 0;color:#c9d1d9;font-size:0.88rem;">
                    The crowd is too bearish relative to the math. There may be value on the YES side.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            """
            <div class="pq-card">
                <span class="pq-badge pq-badge-grey">No Trap Detected</span>
                <p style="margin:0.5rem 0 0;color:#8b949e;font-size:0.88rem;">
                    Public sentiment and mathematical probability are aligned. No action needed.
                </p>
            </div>
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


def render_risk_free_arbs() -> None:
    st.markdown("### 💰 Risk-Free Arbs")
    st.markdown(
        '<p style="color:#8b949e;font-size:0.82rem;margin-top:-0.5rem;">'
        "Compare the same event across two books. If combined cost is under $1, profit is locked."
        "</p>",
        unsafe_allow_html=True,
    )

    if st.button("↻ Refresh Prices", key="refresh_arb"):
        fetch_polymarket_markets.clear()
        fetch_kalshi_markets.clear()
        st.rerun()

    try:
        poly_df = fetch_polymarket_markets()
        kalshi_df = fetch_kalshi_markets()
    except requests.exceptions.RequestException:
        st.error("Unable to reach one of the exchanges. Try again shortly.")
        return
    except (ValueError, json.JSONDecodeError):
        st.error("Price data came back in an unexpected format.")
        return
    except Exception:
        st.error("Something went wrong loading exchange prices.")
        return

    poly_priced = poly_df.dropna(subset=["Yes Price", "No Price"]).copy()
    kalshi_priced = _filter_kalshi_tradeable(kalshi_df)

    if poly_priced.empty or kalshi_priced.empty:
        st.warning("Not enough priced contracts on both books right now.")
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

    st.markdown("#### 1 · Choose Your Markets")
    poly_id = render_searchable_picker(
        "Polymarket Event",
        poly_options,
        "poly_selected",
        show_prices=poly_prices,
    )
    kalshi_ticker = render_searchable_picker(
        "Kalshi Event",
        kalshi_options,
        "kalshi_selected",
        show_prices=kalshi_prices,
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
    net_a, roi_a = _arb_opportunity(cost_a)
    cost_b = poly_no + kalshi_yes
    net_b, roi_b = _arb_opportunity(cost_b)

    st.markdown("#### 2 · Arb Strategies")

    tab_a, tab_b = st.tabs(["Strategy A", "Strategy B"])
    with tab_a:
        _render_arb_split("YES", poly_yes, "NO", kalshi_no, net_a, roi_a, cost_a < 1.0, odds_fmt)
    with tab_b:
        _render_arb_split("NO", poly_no, "YES", kalshi_yes, net_b, roi_b, cost_b < 1.0, odds_fmt)

    if cost_a >= 1.0 and cost_b >= 1.0:
        st.markdown(
            """
            <div class="pq-card">
                <span class="pq-badge pq-badge-grey">No Arb on This Pair</span>
                <p style="margin:0.5rem 0 0;color:#8b949e;font-size:0.88rem;">
                    Combined cost is $1.00 or more on both sides. Keep scanning other matchups.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )


# --------------------------------------------------------------------------- #
# App shell
# --------------------------------------------------------------------------- #

st.set_page_config(
    page_title="POLY-QUANT-v1",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

_inject_global_css()
_init_session()

st.markdown(
    """
    <div class="pq-hero">
        <h1>POLY-QUANT v1</h1>
        <p>Live prediction-market intelligence · Polymarket + Kalshi</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.container():
    st.markdown('<div class="pq-odds-bar">', unsafe_allow_html=True)
    render_odds_format_toggle()
    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    tab_value, tab_audit, tab_trap, tab_arb = st.tabs(
        [
            "🔥 Top Value Plays",
            "⚖️ Audit My Bet",
            "🚨 Trap Detector",
            "💰 Risk-Free Arbs",
        ]
    )

    with tab_value:
        render_top_value_plays()

    with tab_audit:
        render_audit_my_bet()

    with tab_trap:
        render_trap_detector()

    with tab_arb:
        render_risk_free_arbs()


if __name__ == "__main__":
    main()
