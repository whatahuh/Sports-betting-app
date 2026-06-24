"""
POLY-QUANT-v1
=============
A local, tactical quant dashboard for Polymarket prediction-exchange data.

Three operating decks:
    1. Asymmetric NO Hunter  - live Gamma API scan + liquidity filter + strike-zone flagging
    2. The Devigger Engine    - net-settlement expected value calculator + gatekeeper
    3. Sentiment Arbitrage    - narrative divergence model (social vs. implied)

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
# Global configuration & tactical theme
# --------------------------------------------------------------------------- #

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
REQUEST_TIMEOUT = 20  # seconds
CACHE_TTL = 60        # seconds

# Strategy constants
MIN_VOLUME = 5_000.0          # hard liquidity floor (USD)
STRIKE_LO = 0.70              # asymmetric NO strike zone lower bound
STRIKE_HI = 0.85              # asymmetric NO strike zone upper bound
EV_THRESHOLD = 4.5            # minimum EV yield (%) to be playable
WIN_PROB_THRESHOLD = 75.0     # minimum model win probability (%) to be playable
DIVERGENCE_TRIGGER = 20.0     # divergence delta trigger (percentage points)

st.set_page_config(
    page_title="POLY-QUANT-v1",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Dark-mode terminal aesthetic; strip default Streamlit chrome and margins.
st.markdown(
    """
    <style>
        /* Kill default Streamlit chrome */
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        footer {visibility: hidden;}
        .stDeployButton {display: none;}

        /* Tighten the main container into a dense, full-bleed grid */
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1.5rem;
            padding-left: 1.6rem;
            padding-right: 1.6rem;
            max-width: 100%;
        }

        /* Terminal canvas */
        .stApp {
            background-color: #0a0e14;
            color: #c9d1d9;
            font-family: "JetBrains Mono", "SFMono-Regular", "Consolas", monospace;
        }

        /* Headline ticker */
        .pq-banner {
            border: 1px solid #1f2933;
            border-left: 4px solid #3fb950;
            background: linear-gradient(90deg, #0d1117 0%, #0a0e14 100%);
            padding: 0.55rem 1rem;
            margin-bottom: 0.9rem;
            border-radius: 4px;
        }
        .pq-banner h1 {
            font-size: 1.35rem;
            font-weight: 700;
            letter-spacing: 0.16em;
            margin: 0;
            color: #3fb950;
            text-transform: uppercase;
        }
        .pq-banner span {
            font-size: 0.72rem;
            color: #8b949e;
            letter-spacing: 0.08em;
        }

        /* Tabs: dense, monospace, high-contrast */
        .stTabs [data-baseweb="tab-list"] {
            gap: 4px;
            background-color: #0d1117;
            border-radius: 6px;
            padding: 4px;
        }
        .stTabs [data-baseweb="tab"] {
            height: 38px;
            background-color: #11161d;
            border-radius: 4px;
            color: #8b949e;
            font-size: 0.82rem;
            letter-spacing: 0.04em;
            padding: 0 16px;
        }
        .stTabs [aria-selected="true"] {
            background-color: #1f6feb33 !important;
            color: #58a6ff !important;
            border: 1px solid #1f6feb66;
        }

        /* Metric cards */
        [data-testid="stMetric"] {
            background-color: #0d1117;
            border: 1px solid #1f2933;
            border-radius: 6px;
            padding: 0.8rem 1rem;
        }

        /* Dataframe edges */
        [data-testid="stDataFrame"] {
            border: 1px solid #1f2933;
            border-radius: 6px;
        }

        /* Slimmer alert blocks */
        .stAlert {
            border-radius: 6px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="pq-banner">
        <h1>POLY-QUANT-v1</h1>
        <span>TACTICAL PREDICTION-EXCHANGE TERMINAL // ASYMMETRIC NO STRATEGY DESK</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort conversion of arbitrary API values to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = str(value).replace("$", "").replace(",", "").strip()
        if cleaned == "":
            return None
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def parse_outcome_prices(raw: Any) -> tuple[Optional[float], Optional[float]]:
    """
    Extract (yes_price, no_price) from Polymarket's ``outcomePrices`` field.

    Polymarket frequently returns this field as a *double-encoded* JSON string,
    e.g. the literal text ``'["0.23", "0.77"]'`` instead of a real list. It can
    even be encoded twice. This helper unwraps however many layers of JSON
    string encoding are present, then pulls index [0] as the Yes price and
    index [1] as the No price.
    """
    parsed: Any = raw

    # Repeatedly decode while we still have a JSON-looking string.
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

    yes_price = _coerce_float(parsed[0])
    no_price = _coerce_float(parsed[1])
    return yes_price, no_price


# --------------------------------------------------------------------------- #
# Data acquisition (cached scraper)
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=CACHE_TTL, show_spinner="Scanning Polymarket Gamma API...")
def fetch_markets() -> pd.DataFrame:
    """
    Hit the public, unauthenticated Polymarket Gamma markets endpoint and return
    a normalized DataFrame.

    Query parameters: active=true, closed=false, limit=100.
    Cached for 60s (TTL) to avoid hammering the API on UI re-runs.
    """
    params = {"active": "true", "closed": "false", "limit": 100}
    headers = {"User-Agent": "POLY-QUANT-v1/1.0 (+local-dashboard)"}

    response = requests.get(
        GAMMA_MARKETS_URL,
        params=params,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    # Gamma may return a bare list or an object wrapping the list.
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
            market.get("volume")
            or market.get("volumeNum")
            or market.get("volumeClob")
        )
        liquidity = _coerce_float(
            market.get("liquidity") or market.get("liquidityNum")
        )

        rows.append(
            {
                "Question": market.get("question")
                or market.get("title")
                or market.get("slug")
                or "—",
                "Yes Price": yes_price,
                "No Price": no_price,
                "Volume": volume if volume is not None else 0.0,
                "Liquidity": liquidity if liquidity is not None else 0.0,
            }
        )

    df = pd.DataFrame(rows)
    return df


# --------------------------------------------------------------------------- #
# Tab builders
# --------------------------------------------------------------------------- #

def _highlight_strike_zone(row: pd.Series) -> list[str]:
    """Faint-green row highlight when the No price is inside the strike zone."""
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
        f"Live scan • liquidity floor ${MIN_VOLUME:,.0f} • "
        f"strike zone ${STRIKE_LO:.2f}–${STRIKE_HI:.2f} flagged green • "
        f"cache TTL {CACHE_TTL}s"
    )

    top = st.columns([1, 5])
    with top[0]:
        if st.button("🔄 Force Refresh", use_container_width=True):
            fetch_markets.clear()
            st.rerun()

    try:
        raw_df = fetch_markets()
    except requests.exceptions.RequestException as exc:
        st.error(f"🚫 Gamma API request failed: {exc}")
        return
    except (ValueError, json.JSONDecodeError) as exc:
        st.error(f"🚫 Failed to decode Gamma API response: {exc}")
        return

    if raw_df.empty:
        st.warning("No markets returned by the Gamma API.")
        return

    # Drop markets with no usable No price, then apply the hard liquidity filter.
    df = raw_df.dropna(subset=["No Price"]).copy()
    pre_filter = len(df)
    df = df[df["Volume"] >= MIN_VOLUME].copy()

    if df.empty:
        st.warning(
            f"All {pre_filter} priced markets fell below the "
            f"${MIN_VOLUME:,.0f} liquidity floor."
        )
        return

    # Sort entirely from cheapest No to most expensive No.
    df = df.sort_values("No Price", ascending=True).reset_index(drop=True)

    in_zone_count = int(df["No Price"].between(STRIKE_LO, STRIKE_HI).sum())

    metrics = st.columns(4)
    metrics[0].metric("Markets Scanned", f"{len(raw_df):,}")
    metrics[1].metric("Passed Liquidity", f"{len(df):,}")
    metrics[2].metric("In Strike Zone", f"{in_zone_count:,}")
    metrics[3].metric("Cheapest No", f"${df['No Price'].iloc[0]:.2f}")

    styled = (
        df.style
        .apply(_highlight_strike_zone, axis=1)
        .format(
            {
                "Yes Price": lambda v: "—" if pd.isna(v) else f"${v:.2f}",
                "No Price": lambda v: "—" if pd.isna(v) else f"${v:.2f}",
                "Volume": "${:,.0f}",
                "Liquidity": "${:,.0f}",
            }
        )
    )

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={
            "Question": st.column_config.TextColumn("Market", width="large"),
            "Yes Price": st.column_config.TextColumn("Yes"),
            "No Price": st.column_config.TextColumn("No"),
            "Volume": st.column_config.TextColumn("Volume"),
            "Liquidity": st.column_config.TextColumn("Liquidity"),
        },
    )


def render_devigger() -> None:
    st.subheader("🧮 The Devigger Engine")
    st.caption(
        "Net-settlement expected value station • gatekeeper requires "
        f"EV ≥ {EV_THRESHOLD:.1f}% AND win prob ≥ {WIN_PROB_THRESHOLD:.0f}%"
    )

    inputs = st.columns(3)
    with inputs[0]:
        true_win_prob = st.number_input(
            "Model True Win Probability (%)",
            min_value=0.0,
            max_value=100.0,
            value=77.5,
            step=0.5,
            help="Your model's estimated probability that the position wins.",
        )
    with inputs[1]:
        stake = st.number_input(
            "Total Market Stake ($)",
            min_value=0.0,
            value=100.0,
            step=10.0,
            help="Total dollars committed to the position.",
        )
    with inputs[2]:
        share_price = st.number_input(
            "Current Offered Share Price (cents)",
            min_value=0.01,
            max_value=99.99,
            value=50.0,
            step=1.0,
            help="The price per share, in cents, currently offered on the book.",
        )

    # Core math.
    p_win = true_win_prob / 100.0
    p_loss = 1.0 - p_win
    cost = share_price / 100.0
    shares = stake / cost if cost > 0 else 0.0
    profit = (shares * 1.0) - stake  # winning shares settle at $1.00 each

    # Mandatory net-settlement EV formula.
    ev_dollars = (p_win * profit) - (p_loss * stake)
    ev_yield_pct = (ev_dollars / stake * 100.0) if stake > 0 else 0.0

    st.divider()

    cards = st.columns(4)
    cards[0].metric("Shares Purchased", f"{shares:,.2f}")
    cards[1].metric("Net Profit if Win", f"${profit:,.2f}")
    cards[2].metric(
        "Expected Value",
        f"${ev_dollars:,.2f}",
        delta=f"{ev_yield_pct:+.2f}% yield",
    )
    cards[3].metric("EV Yield", f"{ev_yield_pct:+.2f}%")

    st.divider()

    ev_ok = ev_yield_pct >= EV_THRESHOLD
    prob_ok = true_win_prob >= WIN_PROB_THRESHOLD

    if ev_ok and prob_ok:
        st.success(
            "### ✅ PLAYABLE — GREEN LIGHT\n"
            f"Both gates cleared. Plant a **Limit Order at {share_price:.0f}¢**.\n\n"
            f"- EV yield **{ev_yield_pct:+.2f}%** ≥ {EV_THRESHOLD:.1f}% ✔\n"
            f"- True win probability **{true_win_prob:.1f}%** "
            f"≥ {WIN_PROB_THRESHOLD:.0f}% ✔\n"
            f"- Flat expected value **${ev_dollars:,.2f}** on a "
            f"${stake:,.2f} stake."
        )
    else:
        failures: list[str] = []
        if not ev_ok:
            failures.append(
                f"EV yield **{ev_yield_pct:+.2f}%** is below the "
                f"{EV_THRESHOLD:.1f}% threshold "
                f"(short by {EV_THRESHOLD - ev_yield_pct:.2f} pts)."
            )
        if not prob_ok:
            failures.append(
                f"True win probability **{true_win_prob:.1f}%** is below the "
                f"{WIN_PROB_THRESHOLD:.0f}% threshold "
                f"(short by {WIN_PROB_THRESHOLD - true_win_prob:.1f} pts)."
            )
        detail = "\n".join(f"- {f}" for f in failures)
        st.error(
            "### 🚫 HARD PASS — MATH VIOLATION\n"
            "Do not commit capital. Threshold(s) failed:\n\n"
            f"{detail}"
        )


def render_sentiment_arbitrage() -> None:
    st.subheader("🌐 Sentiment Arbitrage")
    st.caption(
        "Narrative divergence model • social echo chambers vs. prediction engine • "
        f"trigger at ±{DIVERGENCE_TRIGGER:.0f} pts"
    )

    sliders = st.columns(2)
    with sliders[0]:
        implied_prob = st.slider(
            "Polymarket Implied Probability %",
            min_value=0.0,
            max_value=100.0,
            value=50.0,
            step=0.5,
        )
    with sliders[1]:
        sentiment = st.slider(
            "Estimated Public Forum Sentiment Score (0-100)",
            min_value=0.0,
            max_value=100.0,
            value=50.0,
            step=0.5,
        )

    delta = sentiment - implied_prob

    st.divider()

    cards = st.columns(3)
    cards[0].metric("Implied Probability", f"{implied_prob:.1f}%")
    cards[1].metric("Public Sentiment", f"{sentiment:.1f}")
    cards[2].metric("Divergence Δ", f"{delta:+.1f} pts")

    st.divider()

    if delta >= DIVERGENCE_TRIGGER:
        st.error(
            "### 🚨 RETAIL TRAP DETECTED\n"
            f"Divergence **{delta:+.1f} pts** ≥ +{DIVERGENCE_TRIGGER:.0f}. "
            "The public herd is far more bullish than the exchange.\n\n"
            "- **Fade the herd completely.**\n"
            "- Let the speculation inflate the **Yes** price.\n"
            "- Execute exclusively on the underpriced **Asymmetric NO** tokens."
        )
    elif delta <= -DIVERGENCE_TRIGGER:
        st.info(
            "### 📉 OVERSOLD ASSET\n"
            f"Divergence **{delta:+.1f} pts** ≤ -{DIVERGENCE_TRIGGER:.0f}. "
            "Public sentiment is far more bearish than the exchange implies.\n\n"
            "- Sharp quantitative advantage on the **Yes** side.\n"
            "- The asset is oversold relative to the prediction engine."
        )
    else:
        st.success(
            "### ⚖️ NO ARBITRAGE EDGE\n"
            f"Divergence **{delta:+.1f} pts** sits inside the "
            f"±{DIVERGENCE_TRIGGER:.0f} pt neutral band. "
            "Narrative and price are aligned — stand down."
        )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

def main() -> None:
    tab_hunter, tab_devig, tab_sentiment = st.tabs(
        [
            "📊 Asymmetric NO Hunter",
            "🧮 The Devigger Engine",
            "🌐 Sentiment Arbitrage",
        ]
    )

    with tab_hunter:
        render_no_hunter()

    with tab_devig:
        render_devigger()

    with tab_sentiment:
        render_sentiment_arbitrage()


if __name__ == "__main__":
    main()
