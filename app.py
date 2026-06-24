"""
POLY-QUANT-v1
=============
Tactical prediction-exchange terminal for Polymarket + Kalshi cross-venue analysis.

Four operating decks:
    1. Asymmetric NO Hunter  - Gamma API scan, liquidity filter, strike-zone flagging
    2. The Devigger Engine     - net-settlement EV calculator + gatekeeper
    3. Sentiment Arbitrage     - narrative divergence model
    4. Cross-Exchange Arb      - Polymarket vs Kalshi dual-scenario arb engine

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pandas as pd
import requests
import streamlit as st

# --------------------------------------------------------------------------- #
# Global configuration
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

# Locked dataframe column widths (px) — keeps price metrics visible on narrow screens.
COL_QUESTION = 200
COL_PRICE = 72
COL_VOLUME = 88

st.set_page_config(
    page_title="POLY-QUANT-v1",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

        .block-container {
            padding-top: 0.9rem;
            padding-bottom: 1.2rem;
            padding-left: max(0.75rem, env(safe-area-inset-left));
            padding-right: max(0.75rem, env(safe-area-inset-right));
            max-width: 100%;
        }

        .stApp {
            background-color: #0a0e14;
            color: #c9d1d9;
            font-family: "JetBrains Mono", "SFMono-Regular", "Consolas", monospace;
        }

        .pq-banner {
            border: 1px solid #1f2933;
            border-left: 4px solid #3fb950;
            background: linear-gradient(90deg, #0d1117 0%, #0a0e14 100%);
            padding: 0.5rem 0.85rem;
            margin-bottom: 0.75rem;
            border-radius: 4px;
        }
        .pq-banner h1 {
            font-size: clamp(1rem, 3.5vw, 1.35rem);
            font-weight: 700;
            letter-spacing: 0.14em;
            margin: 0;
            color: #3fb950;
            text-transform: uppercase;
        }
        .pq-banner span {
            font-size: clamp(0.62rem, 2.2vw, 0.72rem);
            color: #8b949e;
            letter-spacing: 0.06em;
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 3px;
            background-color: #0d1117;
            border-radius: 6px;
            padding: 3px;
            flex-wrap: wrap;
        }
        .stTabs [data-baseweb="tab"] {
            min-height: 36px;
            height: auto;
            background-color: #11161d;
            border-radius: 4px;
            color: #8b949e;
            font-size: clamp(0.68rem, 2.5vw, 0.82rem);
            letter-spacing: 0.03em;
            padding: 6px 10px;
            white-space: normal;
        }
        .stTabs [aria-selected="true"] {
            background-color: #1f6feb33 !important;
            color: #58a6ff !important;
            border: 1px solid #1f6feb66;
        }

        [data-testid="stMetric"] {
            background-color: #0d1117;
            border: 1px solid #1f2933;
            border-radius: 6px;
            padding: 0.65rem 0.8rem;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid #1f2933;
            border-radius: 6px;
            overflow-x: auto;
        }

        .stAlert { border-radius: 6px; }

        .pq-arb-card {
            border: 2px solid #3fb950;
            background: rgba(63, 185, 80, 0.12);
            border-radius: 8px;
            padding: 1rem;
            margin-top: 0.5rem;
        }
        .pq-arb-card h3 {
            color: #3fb950;
            margin: 0 0 0.5rem 0;
            font-size: 1.1rem;
        }

        @media (max-width: 768px) {
            .block-container { padding-top: 0.6rem; }
            [data-testid="stMetric"] { padding: 0.5rem 0.6rem; }
            [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="pq-banner">
        <h1>POLY-QUANT-v1</h1>
        <span>TACTICAL PREDICTION-EXCHANGE TERMINAL // POLY + KALSHI CROSS-VENUE DESK</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Shared column configs (mobile + desktop)
# --------------------------------------------------------------------------- #

def hunter_column_config() -> dict[str, Any]:
    return {
        "Question": st.column_config.TextColumn(
            "Market",
            width=COL_QUESTION,
            help="Contract question text",
        ),
        "Yes Price": st.column_config.NumberColumn(
            "Yes",
            width=COL_PRICE,
            format="$%.2f",
        ),
        "No Price": st.column_config.NumberColumn(
            "No",
            width=COL_PRICE,
            format="$%.2f",
        ),
        "Volume": st.column_config.NumberColumn(
            "Vol",
            width=COL_VOLUME,
            format="$%,.0f",
        ),
        "Liquidity": st.column_config.NumberColumn(
            "Liq",
            width=COL_VOLUME,
            format="$%,.0f",
        ),
    }


# --------------------------------------------------------------------------- #
# Data acquisition
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=CACHE_TTL, show_spinner="Scanning Polymarket Gamma API...")
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


@st.cache_data(ttl=CACHE_TTL, show_spinner="Scanning Kalshi V2 API...")
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

        # Kalshi V2 fixed-point dollar schema — never use legacy cent integers.
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


# --------------------------------------------------------------------------- #
# Tab 1 — Asymmetric NO Hunter
# --------------------------------------------------------------------------- #

def _highlight_strike_zone(row: pd.Series) -> list[str]:
    no_price = row.get("No Price")
    in_zone = (
        no_price is not None
        and pd.notna(no_price)
        and STRIKE_LO <= float(no_price) <= STRIKE_HI
    )
    style = "background-color: rgba(63, 185, 80, 0.18);" if in_zone else ""
    return [style] * len(row)


def render_no_hunter() -> None:
    st.subheader("📊 Asymmetric NO Hunter")
    st.caption(
        f"Gamma scan • vol ≥ ${MIN_VOLUME:,.0f} • "
        f"strike ${STRIKE_LO:.2f}–${STRIKE_HI:.2f} • TTL {CACHE_TTL}s"
    )

    if st.button("🔄 Force Refresh", key="refresh_poly"):
        fetch_polymarket_markets.clear()
        st.rerun()

    try:
        raw_df = fetch_polymarket_markets()
    except requests.exceptions.RequestException as exc:
        st.error(f"🚫 Polymarket API unreachable: {exc}")
        return
    except (ValueError, json.JSONDecodeError) as exc:
        st.error(f"🚫 Polymarket response decode failed: {exc}")
        return
    except Exception as exc:
        st.error(f"🚫 Unexpected Polymarket error: {exc}")
        return

    if raw_df.empty:
        st.warning("No markets returned by the Gamma API.")
        return

    display_cols = ["Question", "Yes Price", "No Price", "Volume", "Liquidity"]
    df = raw_df.dropna(subset=["No Price"]).copy()
    pre_filter = len(df)
    df = df[df["Volume"] >= MIN_VOLUME].copy()

    if df.empty:
        st.warning(
            f"All {pre_filter} priced markets fell below the "
            f"${MIN_VOLUME:,.0f} liquidity floor."
        )
        return

    df = df.sort_values("No Price", ascending=True).reset_index(drop=True)
    in_zone_count = int(df["No Price"].between(STRIKE_LO, STRIKE_HI).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scanned", f"{len(raw_df):,}")
    m2.metric("Passed Filter", f"{len(df):,}")
    m3.metric("Strike Zone", f"{in_zone_count:,}")
    m4.metric("Cheapest No", f"${df['No Price'].iloc[0]:.2f}")

    styled = df[display_cols].style.apply(_highlight_strike_zone, axis=1)

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        height=min(560, 44 + len(df) * 36),
        column_config=hunter_column_config(),
    )


# --------------------------------------------------------------------------- #
# Tab 2 — The Devigger Engine
# --------------------------------------------------------------------------- #

def render_devigger() -> None:
    st.subheader("🧮 The Devigger Engine")
    st.caption(
        f"Net-settlement EV • playable if EV ≥ {EV_THRESHOLD:.1f}% "
        f"AND win prob ≥ {WIN_PROB_THRESHOLD:.0f}%"
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        true_win_prob = st.number_input(
            "Model True Win Probability (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
        )
    with c2:
        stake = st.number_input(
            "Total Market Stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
        )
    with c3:
        share_price = st.number_input(
            "Current Offered Share Price (cents)",
            min_value=0.01,
            max_value=99.99,
            value=50.0,
            step=1.0,
        )

    p_win = true_win_prob / 100.0
    p_loss = 1.0 - p_win
    cost = share_price / 100.0
    shares = stake / cost if cost > 0 else 0.0
    profit = (shares * 1.0) - stake
    ev_dollars = (p_win * profit) - (p_loss * stake)
    ev_yield_pct = (ev_dollars / stake * 100.0) if stake > 0 else 0.0

    st.divider()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Shares", f"{shares:,.2f}")
    k2.metric("Profit if Win", f"${profit:,.2f}")
    k3.metric("Expected Value", f"${ev_dollars:,.2f}", delta=f"{ev_yield_pct:+.2f}%")
    k4.metric("EV Yield", f"{ev_yield_pct:+.2f}%")

    st.divider()

    ev_ok = ev_yield_pct >= EV_THRESHOLD
    prob_ok = true_win_prob >= WIN_PROB_THRESHOLD

    if ev_ok and prob_ok:
        st.success(
            "### ✅ PLAYABLE — GREEN LIGHT\n"
            f"Plant a **Limit Order at {share_price:.0f}¢**.\n\n"
            f"- EV yield **{ev_yield_pct:+.2f}%** ≥ {EV_THRESHOLD:.1f}% ✔\n"
            f"- Win probability **{true_win_prob:.1f}%** ≥ {WIN_PROB_THRESHOLD:.0f}% ✔\n"
            f"- Flat EV **${ev_dollars:,.2f}** on ${stake:,.2f} stake."
        )
    else:
        failures: list[str] = []
        if not ev_ok:
            failures.append(
                f"EV yield **{ev_yield_pct:+.2f}%** < {EV_THRESHOLD:.1f}% "
                f"(short {EV_THRESHOLD - ev_yield_pct:.2f} pts)."
            )
        if not prob_ok:
            failures.append(
                f"Win probability **{true_win_prob:.1f}%** < {WIN_PROB_THRESHOLD:.0f}% "
                f"(short {WIN_PROB_THRESHOLD - true_win_prob:.1f} pts)."
            )
        st.error(
            "### 🚫 HARD PASS — MATH VIOLATION\n"
            + "\n".join(f"- {f}" for f in failures)
        )


# --------------------------------------------------------------------------- #
# Tab 3 — Sentiment Arbitrage
# --------------------------------------------------------------------------- #

def render_sentiment_arbitrage() -> None:
    st.subheader("🌐 Sentiment Arbitrage")
    st.caption(f"Narrative divergence • trigger at ±{DIVERGENCE_TRIGGER:.0f} pts")

    s1, s2 = st.columns(2)
    with s1:
        implied_prob = st.slider(
            "Polymarket Implied Probability %",
            0.0, 100.0, 50.0, 0.5,
        )
    with s2:
        sentiment = st.slider(
            "Estimated Public Forum Sentiment Score (0-100)",
            0.0, 100.0, 50.0, 0.5,
        )

    delta = sentiment - implied_prob

    st.divider()
    d1, d2, d3 = st.columns(3)
    d1.metric("Implied Prob", f"{implied_prob:.1f}%")
    d2.metric("Sentiment", f"{sentiment:.1f}")
    d3.metric("Divergence Δ", f"{delta:+.1f} pts")
    st.divider()

    if delta >= DIVERGENCE_TRIGGER:
        st.error(
            "### 🚨 RETAIL TRAP DETECTED\n"
            f"Δ **{delta:+.1f}** ≥ +{DIVERGENCE_TRIGGER:.0f}. "
            "Fade the herd — let Yes inflate, execute **Asymmetric NO** only."
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.info(
            "### 📉 OVERSOLD ASSET\n"
            f"Δ **{delta:+.1f}** ≤ -{DIVERGENCE_TRIGGER:.0f}. "
            "Quantitative advantage on the **Yes** side."
        )
    else:
        st.success(
            f"### ⚖️ NO EDGE\nΔ **{delta:+.1f}** inside ±{DIVERGENCE_TRIGGER:.0f} band — stand down."
        )


# --------------------------------------------------------------------------- #
# Tab 4 — Cross-Exchange Arb
# --------------------------------------------------------------------------- #

def _select_label(text: str, max_len: int = 72) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= max_len else clean[: max_len - 1] + "…"


def _arb_opportunity(total_cost: float) -> tuple[float, float]:
    """Return (net_return, roi_pct) for a paired position settling at $1.00."""
    net_return = 1.0 - total_cost
    roi_pct = (net_return / total_cost * 100.0) if total_cost > 0 else 0.0
    return net_return, roi_pct


def render_cross_exchange_arb() -> None:
    st.subheader("🔄 Cross-Exchange Arb")
    st.caption(
        "Polymarket Gamma + Kalshi V2 • dollar-string schema • "
        "dual-scenario settlement arb"
    )

    refresh_cols = st.columns([1, 5])
    with refresh_cols[0]:
        if st.button("🔄 Refresh Both", key="refresh_arb"):
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            st.rerun()

    try:
        poly_df = fetch_polymarket_markets()
        kalshi_df = fetch_kalshi_markets()
    except requests.exceptions.RequestException as exc:
        st.error(f"🚫 Exchange API unreachable: {exc}")
        return
    except (ValueError, json.JSONDecodeError) as exc:
        st.error(f"🚫 API response decode failed: {exc}")
        return
    except Exception as exc:
        st.error(f"🚫 Unexpected fetch error: {exc}")
        return

    poly_priced = poly_df.dropna(subset=["Yes Price", "No Price"]).copy()
    kalshi_priced = kalshi_df.dropna(subset=["Kalshi YES Cost", "Kalshi NO Cost"]).copy()

    if poly_priced.empty:
        st.warning("No priced Polymarket contracts available.")
        return
    if kalshi_priced.empty:
        st.warning("No priced Kalshi contracts available.")
        return

    poly_options = {
        row["id"]: _select_label(row["Question"])
        for _, row in poly_priced.iterrows()
    }
    kalshi_options = {
        row["ticker"]: _select_label(row["Title"])
        for _, row in kalshi_priced.iterrows()
    }

    pick_l, pick_r = st.columns(2)
    with pick_l:
        poly_id = st.selectbox(
            "Polymarket Contract",
            options=list(poly_options.keys()),
            format_func=lambda k: poly_options[k],
        )
    with pick_r:
        kalshi_ticker = st.selectbox(
            "Kalshi Contract",
            options=list(kalshi_options.keys()),
            format_func=lambda k: kalshi_options[k],
        )

    poly_row = poly_priced.loc[poly_priced["id"] == poly_id].iloc[0]
    kalshi_row = kalshi_priced.loc[kalshi_priced["ticker"] == kalshi_ticker].iloc[0]

    poly_yes = float(poly_row["Yes Price"])
    poly_no = float(poly_row["No Price"])
    kalshi_yes = float(kalshi_row["Kalshi YES Cost"])
    kalshi_no = float(kalshi_row["Kalshi NO Cost"])

    st.divider()

    quote_cols = st.columns(4)
    quote_cols[0].metric("Poly YES", f"${poly_yes:.2f}")
    quote_cols[1].metric("Poly NO", f"${poly_no:.2f}")
    quote_cols[2].metric("Kalshi YES", f"${kalshi_yes:.4f}")
    quote_cols[3].metric("Kalshi NO", f"${kalshi_no:.4f}")

    # Strategy A: Poly YES + Kalshi NO
    cost_a = poly_yes + kalshi_no
    net_a, roi_a = _arb_opportunity(cost_a)

    # Strategy B: Poly NO + Kalshi YES
    cost_b = poly_no + kalshi_yes
    net_b, roi_b = _arb_opportunity(cost_b)

    st.markdown("#### Dual-Scenario Arb Matrix")

    arb_df = pd.DataFrame(
        [
            {
                "Strategy": "A: Poly YES + Kalshi NO",
                "Poly Leg": f"YES @ ${poly_yes:.2f}",
                "Kalshi Leg": f"NO @ ${kalshi_no:.4f}",
                "Total Cost": cost_a,
                "Net Return": net_a,
                "ROI %": roi_a,
                "Arb": cost_a < 1.0,
            },
            {
                "Strategy": "B: Poly NO + Kalshi YES",
                "Poly Leg": f"NO @ ${poly_no:.2f}",
                "Kalshi Leg": f"YES @ ${kalshi_yes:.4f}",
                "Total Cost": cost_b,
                "Net Return": net_b,
                "ROI %": roi_b,
                "Arb": cost_b < 1.0,
            },
        ]
    )

    st.dataframe(
        arb_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Strategy": st.column_config.TextColumn("Strategy", width=180),
            "Poly Leg": st.column_config.TextColumn("Poly", width=110),
            "Kalshi Leg": st.column_config.TextColumn("Kalshi", width=110),
            "Total Cost": st.column_config.NumberColumn("Cost", width=72, format="$%.4f"),
            "Net Return": st.column_config.NumberColumn("Net", width=72, format="$%.4f"),
            "ROI %": st.column_config.NumberColumn("ROI", width=72, format="%.2f%%"),
            "Arb": st.column_config.CheckboxColumn("Arb?", width=60),
        },
    )

    arb_found = False

    if cost_a < 1.0:
        arb_found = True
        st.markdown(
            f"""
            <div class="pq-arb-card">
                <h3>💰 RISK-FREE ARB — STRATEGY A</h3>
                <p>Buy Polymarket <b>YES</b> + Kalshi <b>NO</b></p>
                <p>Total cost <b>${cost_a:.4f}</b> → guaranteed net <b>${net_a:.4f}</b>
                per $1 settlement (<b>{roi_a:.2f}% ROI</b>)</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns(2)
        c1.metric("Guaranteed Net Return", f"${net_a:.4f}")
        c2.metric("ROI", f"{roi_a:.2f}%")

    if cost_b < 1.0:
        arb_found = True
        st.markdown(
            f"""
            <div class="pq-arb-card">
                <h3>💰 RISK-FREE ARB — STRATEGY B</h3>
                <p>Buy Polymarket <b>NO</b> + Kalshi <b>YES</b></p>
                <p>Total cost <b>${cost_b:.4f}</b> → guaranteed net <b>${net_b:.4f}</b>
                per $1 settlement (<b>{roi_b:.2f}% ROI</b>)</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns(2)
        c1.metric("Guaranteed Net Return", f"${net_b:.4f}")
        c2.metric("ROI", f"{roi_b:.2f}%")

    if not arb_found:
        st.warning(
            "No risk-free arb on this pair. Both strategies cost ≥ $1.00 at current quotes."
        )


# --------------------------------------------------------------------------- #
# Main layout
# --------------------------------------------------------------------------- #

def main() -> None:
    tab_hunter, tab_devig, tab_sentiment, tab_arb = st.tabs(
        [
            "📊 Asymmetric NO Hunter",
            "🧮 The Devigger Engine",
            "🌐 Sentiment Arbitrage",
            "🔄 Cross-Exchange Arb",
        ]
    )

    with tab_hunter:
        render_no_hunter()

    with tab_devig:
        render_devigger()

    with tab_sentiment:
        render_sentiment_arbitrage()

    with tab_arb:
        render_cross_exchange_arb()


if __name__ == "__main__":
    main()
