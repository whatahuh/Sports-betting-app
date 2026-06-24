"""Tab 2: Audit My Bet — simplified 3-field input with PLAYABLE/AVOID verdict."""
from __future__ import annotations

from typing import Any

import streamlit as st

from polyquant.config import EV_THRESHOLD, WIN_PROB_THRESHOLD
from polyquant.quant.ev import calc_ev_dollars
from polyquant.quant.kelly import fractional_kelly
from polyquant.ui.components import render_tab_header, render_stat_grid, stat_tile


def _render_verdict(prob_ok: bool, ev_ok: bool, ev_yield_pct: float) -> None:
    if prob_ok and ev_ok:
        st.markdown(
            '<div class="pq-verdict-playable">PLAYABLE</div>',
            unsafe_allow_html=True,
        )
    elif ev_yield_pct > 0:
        st.markdown(
            '<div class="pq-verdict-avoid" style="background:rgba(250,176,5,0.12);border-color:rgba(250,176,5,0.3);">'
            '<span style="color:#fab005;">MARGINAL</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="pq-verdict-avoid">AVOID</div>',
            unsafe_allow_html=True,
        )


def _render_rationale(
    true_win_prob: float,
    stake: float,
    share_price: float,
    ev_dollars: float,
    ev_yield_pct: float,
    kelly_pct: float,
    prob_ok: bool,
    ev_ok: bool,
) -> str:
    market_implied = share_price
    return f"""
**Your inputs**
- True chance of winning: **{true_win_prob:.1f}%**
- Market price: **{share_price:.1f}¢** (implied **{market_implied:.1f}%**)
- Bet size: **${stake:,.2f}**

**What the math says**
- Expected value: **${ev_dollars:+,.2f}** on ${stake:,.2f}
- Your edge: **{ev_yield_pct:+.2f}%**
- Suggested bet size: **{kelly_pct:.1f}%** of bankroll

**Gate checks**
- Win probability (>= {WIN_PROB_THRESHOLD:.0f}%): **{"PASS" if prob_ok else "FAIL"}**
- EV edge (>= {EV_THRESHOLD:.1f}%): **{"PASS" if ev_ok else "FAIL"}**

**Bottom line**
{"Both gates clear — this is a playable bet. Size within Kelly discipline." if prob_ok and ev_ok else "One or more gates failed — pass or wait for a better line."}
"""


def render_tab(tab: Any) -> None:
    with tab:
        render_tab_header(
            "Audit My Bet",
            "Check any bet before you place it — enter your numbers and get a clear verdict.",
            steps=[
                "Enter what you think the true chance is (your estimate).",
                "Enter the market price in cents and your bet size.",
                "Green PLAYABLE = go. Red AVOID = pass. It's that simple.",
            ],
        )

        st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            true_win_prob = st.slider(
                "True chance (%)",
                min_value=1.0,
                max_value=99.0,
                value=77.5,
                step=0.5,
                help="Your honest estimate of the probability this bet wins.",
            )
        with col2:
            share_price = st.number_input(
                "Market price (¢)",
                min_value=1.0,
                max_value=99.0,
                value=50.0,
                step=1.0,
                help="The price you'd pay per share, in cents.",
            )
        c1, c2, _ = st.columns(3)
        with c1:
            stake = st.number_input(
                "Bet size ($)",
                min_value=1.0,
                value=100.0,
                step=10.0,
            )
        st.markdown("</div>", unsafe_allow_html=True)

        ev_dollars, ev_yield_pct = calc_ev_dollars(true_win_prob, stake, share_price)
        kelly_pct = fractional_kelly(true_win_prob, share_price)
        prob_ok = true_win_prob >= WIN_PROB_THRESHOLD
        ev_ok = ev_yield_pct >= EV_THRESHOLD

        _render_verdict(prob_ok, ev_ok, ev_yield_pct)

        edge_display = f"+{ev_yield_pct:.2f}%" if ev_yield_pct >= 0 else f"{ev_yield_pct:.2f}%"
        render_stat_grid(
            [
                stat_tile("True Probability", f"{true_win_prob:.1f}%", "Your estimate", "blue"),
                stat_tile("Your Edge", edge_display, "Net EV after fees", "green" if ev_ok else "red"),
                stat_tile("Suggested Bet Size", f"{kelly_pct:.1f}%", "Of bankroll", "neutral"),
                stat_tile("Expected Value", f"${ev_dollars:+,.2f}", f"On ${stake:,.0f} stake", "green" if ev_dollars > 0 else "red"),
            ],
            cols=4,
        )

        with st.expander("View full rationale", expanded=False):
            rationale = _render_rationale(
                true_win_prob, stake, share_price,
                ev_dollars, ev_yield_pct, kelly_pct,
                prob_ok, ev_ok,
            )
            st.markdown(rationale)
