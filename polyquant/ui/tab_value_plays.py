"""Tab 1: Top Value Plays — merged dashboard + explore + ranked value cards."""
from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

from polyquant.config import (
    EXPLORE_CATEGORIES,
    EXPLORE_PAGE_SIZE,
    EXPLORE_SOURCES,
    EXPLORE_SPORTS_TYPES,
    VALUE_PLAYS_EV_EDGE_MIN,
    VALUE_PLAYS_MAX,
    VALUE_PLAYS_WIN_MIN,
)
from polyquant.api.catalog import build_explore_catalog
from polyquant.api.polymarket import fetch_polymarket_markets
from polyquant.api.kalshi import fetch_kalshi_markets, fetch_kalshi_player_props
from polyquant.quant.filters import filter_value_plays
from polyquant.quant.kelly import fractional_kelly
from polyquant.quant.shin import model_probability
from polyquant.ui.components import (
    format_odds_display,
    get_odds_format,
    render_stat_grid,
    render_tab_header,
    short_title,
    stat_tile,
)


def _collect_market_pulse() -> dict[str, Any]:
    pulse: dict[str, Any] = {
        "poly_count": 0,
        "kalshi_count": 0,
        "catalog_count": 0,
        "value_plays": 0,
        "best_edge": None,
        "total_vp_volume": 0.0,
        "avg_edge": None,
        "data_ok": True,
    }
    try:
        poly_df = fetch_polymarket_markets()
        kalshi_main = fetch_kalshi_markets()
        kalshi_props = fetch_kalshi_player_props()
        kalshi_df = pd.concat([kalshi_main, kalshi_props], ignore_index=True).drop_duplicates(
            subset=["ticker"]
        )
        catalog = build_explore_catalog()
        vp_df = filter_value_plays(poly_df)

        pulse["poly_count"] = len(poly_df)
        pulse["kalshi_count"] = len(kalshi_df)
        pulse["catalog_count"] = len(catalog)
        pulse["value_plays"] = len(vp_df)
        if not vp_df.empty:
            pulse["best_edge"] = float(vp_df["Net EV Edge %"].iloc[0])
            pulse["avg_edge"] = float(vp_df["Net EV Edge %"].mean())
            pulse["total_vp_volume"] = float(vp_df["Volume"].sum())
    except Exception:
        pulse["data_ok"] = False
    return pulse


def _render_value_play_card(row: pd.Series, rank: int) -> None:
    no_p = float(row["No Price"])
    yes_p = float(row.get("Yes Price", 0) or 0)
    true_yes, true_no = model_probability(yes_p, no_p)
    model_win = true_no * 100.0
    net_edge = float(row["Net EV Edge %"])
    implied_pct = no_p * 100.0
    volume = float(row.get("Volume", 0) or 0)
    kelly_pct = fractional_kelly(model_win, no_p * 100.0, volume)

    card_cls = "pq-value-card pq-value-card-elite" if rank == 1 else "pq-value-card pq-value-card-hot"
    rank_cls = "pq-rank-badge pq-rank-badge-elite" if rank == 1 else "pq-rank-badge"
    rank_label = "Best Play #1" if rank == 1 else f"Edge #{rank}"
    event = html.escape(str(row["Question"]))

    st.markdown(
        f"""
        <div class="{card_cls}">
            <span class="{rank_cls}">{rank_label}</span>
            <p class="pq-event-name">{event}</p>
            <div class="pq-cta-pill">Bet NO at ${no_p:.2f}</div>
            <div class="pq-metric-row">
                <span class="pq-ev-badge">+{net_edge:.2f}% Edge</span>
                <span>Model Prob <strong>{model_win:.1f}%</strong></span>
                <span>Market <strong>{implied_pct:.1f}%</strong></span>
                <span>Kelly <strong>{kelly_pct:.1f}%</strong></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_value_plays_table(df: pd.DataFrame) -> None:
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
            "Rank": st.column_config.NumberColumn("#", width="small", format="%d"),
            "Market": st.column_config.TextColumn("Market", width="large"),
            "Market Line": st.column_config.TextColumn("Line", width="medium"),
            "Implied %": st.column_config.ProgressColumn("Implied", format="%.1f%%", min_value=0, max_value=100, width="medium"),
            "True Prob": st.column_config.ProgressColumn("True Prob", format="%.1f%%", min_value=0, max_value=100, width="medium"),
            "Quant Edge": st.column_config.TextColumn("Edge", width="small"),
            "NO ¢": st.column_config.NumberColumn("NO ¢", format="%.1f", width="small"),
            "Volume": st.column_config.NumberColumn("Volume", format="$%,.0f", width="small"),
        },
    )


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


def _render_matchup_feed(page_df: pd.DataFrame, odds_fmt: str) -> None:
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


def render_tab(tab: Any) -> None:
    with tab:
        render_tab_header(
            "Top Value Plays",
            f"Elite edges ranked by net EV — model prob >{VALUE_PLAYS_WIN_MIN:.0f}%, "
            f"edge ≥{VALUE_PLAYS_EV_EDGE_MIN:.0f}%, top {VALUE_PLAYS_MAX} only.",
            steps=[
                "Ranked cards show the sharpest NO-side edges with plain English actions.",
                "Compare model probability vs market implied to spot mispricing.",
                "Browse the full market catalog below to explore all live contracts.",
            ],
        )

        if st.button("Refresh markets", key="refresh_value_plays", type="primary"):
            fetch_polymarket_markets.clear()
            fetch_kalshi_markets.clear()
            fetch_kalshi_player_props.clear()
            build_explore_catalog.clear()
            st.rerun()

        pulse = _collect_market_pulse()
        if not pulse["data_ok"]:
            st.error("Market data temporarily unavailable — tap refresh or try again shortly.")
            return

        best_edge = pulse["best_edge"]
        avg_edge = pulse["avg_edge"]
        render_stat_grid(
            [
                stat_tile("Live Markets", f"{pulse['catalog_count']:,}", "Polymarket + Kalshi", "blue"),
                stat_tile("Value Plays", str(pulse["value_plays"]), f"Top {VALUE_PLAYS_MAX} edges", "green"),
                stat_tile(
                    "Best Edge",
                    f"+{best_edge:.1f}%" if best_edge is not None else "—",
                    "Highest net EV",
                    "green" if best_edge and best_edge >= VALUE_PLAYS_EV_EDGE_MIN else "neutral",
                ),
                stat_tile(
                    "Avg Edge",
                    f"+{avg_edge:.1f}%" if avg_edge is not None else "—",
                    "Across active plays",
                    "blue",
                ),
            ],
            cols=4,
        )

        # Value plays cards
        try:
            raw_df = fetch_polymarket_markets()
        except Exception:
            st.error("Markets unavailable — try refreshing.")
            return

        if raw_df.empty:
            st.warning("No active markets found.")
            return

        search = st.session_state.get("global_search_query", "")
        df = filter_value_plays(raw_df)
        if search.strip():
            df = df[df["Question"].str.contains(search.strip(), case=False, na=False)].copy()

        if df.empty:
            st.markdown(
                """
                <div class="pq-value-card" style="text-align:center;padding:2.5rem 1.5rem;">
                    <p class="pq-event-name" style="margin-bottom:0.5rem;">No elite edges detected</p>
                    <p style="color:var(--pq-text-muted);font-size:0.95rem;line-height:1.6;margin:0;">
                        Markets look efficient right now. Check back after line moves or browse below for ideas.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            for rank, (_, row) in enumerate(df.iterrows(), 1):
                _render_value_play_card(row, rank)

            with st.expander("View as table", expanded=False):
                _render_value_plays_table(df)

        # Explore catalog section
        st.markdown("---")
        st.markdown('<p class="pq-section-label">Explore Markets</p>', unsafe_allow_html=True)

        try:
            catalog = build_explore_catalog()
        except Exception:
            st.error("Could not load the market catalog.")
            return

        if catalog.empty:
            st.warning("No markets available to explore right now.")
            return

        query = st.session_state.get("global_search_query", "").strip().lower()
        col1, col2, col3 = st.columns(3)
        with col1:
            category = st.selectbox("Category", options=list(EXPLORE_CATEGORIES), key="explore_category")
        with col2:
            source = st.selectbox("Source", options=list(EXPLORE_SOURCES), key="explore_source")
        with col3:
            sports_type = "All"
            if category in ("Sports", "Player Props"):
                sports_type = st.selectbox("Market Type", options=list(EXPLORE_SPORTS_TYPES), key="explore_sports_type")

        filtered = _filter_explore_catalog(catalog, query, category or "All", sports_type or "All", source or "Both")

        if filtered.empty:
            st.info("No markets match your filters.")
            return

        odds_fmt = get_odds_format()
        total_pages = max(1, (len(filtered) + EXPLORE_PAGE_SIZE - 1) // EXPLORE_PAGE_SIZE)
        page = min(st.session_state.explore_page, total_pages - 1)
        st.session_state.explore_page = page
        start = page * EXPLORE_PAGE_SIZE
        page_df = filtered.iloc[start : start + EXPLORE_PAGE_SIZE]

        st.caption(f"{len(filtered):,} markets · Page {page + 1} of {total_pages}")
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
