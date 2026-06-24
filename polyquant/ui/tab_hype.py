"""Tab 3: Hype vs Reality — automated sentiment from Polymarket data + top divergences."""
from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

from polyquant.config import DIVERGENCE_TRIGGER
from polyquant.api.polymarket import fetch_polymarket_markets
from polyquant.quant.shin import model_probability
from polyquant.ui.components import (
    render_stat_grid,
    render_tab_header,
    short_title,
    stat_tile,
)


def _compute_divergences(df: pd.DataFrame) -> pd.DataFrame:
    """Compute hype (market YES price) vs reality (Shin's method) for all markets."""
    priced = df.dropna(subset=["Yes Price", "No Price"]).copy()
    if priced.empty:
        return pd.DataFrame()

    priced = priced[
        (priced["Yes Price"] > 0.01) & (priced["Yes Price"] < 0.99) &
        (priced["No Price"] > 0.01) & (priced["No Price"] < 0.99)
    ].copy()

    if priced.empty:
        return pd.DataFrame()

    results = []
    for _, row in priced.iterrows():
        yes_p = float(row["Yes Price"])
        no_p = float(row["No Price"])
        volume = float(row.get("Volume", 0) or 0)

        true_yes, true_no = model_probability(yes_p, no_p)
        hype_pct = yes_p * 100.0
        reality_pct = true_yes * 100.0
        divergence = hype_pct - reality_pct

        results.append({
            "Question": str(row.get("Question", "")),
            "Yes Price": yes_p,
            "No Price": no_p,
            "Volume": volume,
            "Hype %": round(hype_pct, 1),
            "Reality %": round(reality_pct, 1),
            "Divergence": round(divergence, 1),
            "Abs Divergence": abs(round(divergence, 1)),
        })

    return pd.DataFrame(results).sort_values("Abs Divergence", ascending=False).reset_index(drop=True)


def _render_divergence_card(row: pd.Series, rank: int) -> None:
    question = html.escape(short_title(str(row["Question"]), 80))
    hype = float(row["Hype %"])
    reality = float(row["Reality %"])
    div_val = float(row["Divergence"])
    volume = float(row["Volume"])

    if div_val >= DIVERGENCE_TRIGGER:
        signal = "Overhyped"
        signal_cls = "pq-badge pq-badge-amber"
        signal_detail = "Market price is inflated vs model — consider fading"
    elif div_val <= -DIVERGENCE_TRIGGER:
        signal = "Under the Radar"
        signal_cls = "pq-badge pq-badge-blue"
        signal_detail = "Market is sleeping on this — YES may be cheap"
    else:
        signal = "Aligned"
        signal_cls = "pq-badge pq-badge-grey"
        signal_detail = "No significant divergence"

    hype_width = max(2, min(hype, 98))
    reality_width = max(2, min(reality, 98))

    st.markdown(
        f"""
        <div class="pq-hype-card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">
                <span class="pq-divergence-badge">#{rank}</span>
                <span class="{signal_cls}">{signal}</span>
            </div>
            <p class="pq-event-name" style="margin:0.25rem 0 0.75rem;">{question}</p>
            <div class="pq-hype-bars">
                <div style="margin-bottom:0.5rem;">
                    <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--pq-text-muted);margin-bottom:0.25rem;">
                        <span>Market Says (Hype)</span>
                        <span>{hype:.1f}%</span>
                    </div>
                    <div style="background:var(--pq-surface);border-radius:4px;height:8px;overflow:hidden;">
                        <div style="width:{hype_width}%;height:100%;background:var(--pq-amber);border-radius:4px;"></div>
                    </div>
                </div>
                <div>
                    <div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--pq-text-muted);margin-bottom:0.25rem;">
                        <span>Model Says (Reality)</span>
                        <span>{reality:.1f}%</span>
                    </div>
                    <div style="background:var(--pq-surface);border-radius:4px;height:8px;overflow:hidden;">
                        <div style="width:{reality_width}%;height:100%;background:var(--pq-accent);border-radius:4px;"></div>
                    </div>
                </div>
            </div>
            <div style="display:flex;justify-content:space-between;margin-top:0.75rem;font-size:0.8rem;">
                <span style="color:var(--pq-text-muted);">Divergence: <strong style="color:var(--pq-text);">{div_val:+.1f}%</strong></span>
                <span style="color:var(--pq-text-dim);">Vol: ${volume:,.0f}</span>
            </div>
            <p style="color:var(--pq-text-muted);font-size:0.78rem;margin:0.5rem 0 0;">{signal_detail}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_tab(tab: Any) -> None:
    with tab:
        render_tab_header(
            "Hype vs Reality",
            "Spot narrative bubbles — when what the crowd believes diverges from what the math says.",
            steps=[
                "Top divergences are auto-detected from live Polymarket data.",
                "Hype = market YES price (what people are paying). Reality = Shin's debiased probability.",
                f"A gap of {DIVERGENCE_TRIGGER:.0f}%+ signals the crowd is wrong — fade or follow.",
            ],
        )

        if st.button("Refresh markets", key="refresh_hype", type="primary"):
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

        divergences = _compute_divergences(raw_df)
        if divergences.empty:
            st.info("No priced markets to analyze right now.")
            return

        big_gaps = divergences[divergences["Abs Divergence"] >= DIVERGENCE_TRIGGER]
        biggest = float(divergences["Abs Divergence"].iloc[0]) if not divergences.empty else 0
        avg_div = float(divergences["Abs Divergence"].mean())

        render_stat_grid(
            [
                stat_tile("Markets Scanned", f"{len(divergences):,}", "With valid prices", "blue"),
                stat_tile("Big Divergences", str(len(big_gaps)), f"Gap >= {DIVERGENCE_TRIGGER:.0f}%", "amber" if len(big_gaps) > 0 else "neutral"),
                stat_tile("Biggest Gap", f"{biggest:.1f}%", "Largest divergence", "green" if biggest >= DIVERGENCE_TRIGGER else "neutral"),
                stat_tile("Avg Gap", f"{avg_div:.1f}%", "Across all markets", "neutral"),
            ],
            cols=4,
        )

        search = st.session_state.get("global_search_query", "").strip()
        display_df = divergences.copy()
        if search:
            display_df = display_df[display_df["Question"].str.contains(search, case=False, na=False)]

        st.markdown('<p class="pq-section-label">Top Divergences</p>', unsafe_allow_html=True)

        show_count = st.slider("Show top N", min_value=3, max_value=20, value=5, key="hype_top_n")
        top = display_df.head(show_count)

        if top.empty:
            st.info("No divergences match your search.")
            return

        for rank, (_, row) in enumerate(top.iterrows(), 1):
            _render_divergence_card(row, rank)
