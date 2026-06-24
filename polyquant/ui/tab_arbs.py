"""Tab 4: Risk-Free Arbs — auto-scan + manual picker with plain English execution."""
from __future__ import annotations

import html
from typing import Any

import pandas as pd
import streamlit as st

from polyquant.config import DEFAULT_ARB_STAKE, PLATFORM_FEE_PCT
from polyquant.api.polymarket import fetch_polymarket_markets
from polyquant.api.kalshi import fetch_kalshi_markets, fetch_kalshi_player_props
from polyquant.quant.arb import arb_opportunity, build_arb_strategy, best_arb_strategy
from polyquant.quant.ev import _signed_money
from polyquant.db.repository import save_arb_basket, list_arb_baskets, get_basket_summary
from polyquant.db.schema import initialize_db
from polyquant.ui.components import (
    book_price_hint,
    filter_kalshi_tradeable,
    format_odds_display,
    get_odds_format,
    render_kalshi_suggestions,
    render_searchable_picker,
    render_stat_grid,
    render_tab_header,
    select_label,
    short_title,
    stat_tile,
    sync_kalshi_auto_suggest,
    title_match_score,
)


def _render_cross_book_odds(
    poly_row: pd.Series,
    kalshi_row: pd.Series,
    odds_fmt: str,
) -> None:
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

    poly_title = html.escape(select_label(str(poly_row["Question"]), 80))
    kalshi_title = html.escape(select_label(str(kalshi_row["Title"]), 80))

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


def _render_arb_spotlight(strategy: dict[str, Any], odds_fmt: str) -> None:
    is_arb = bool(strategy["is_arb"])
    cls = "live" if is_arb else "dead"
    kicker = "TAKE THIS ARB" if is_arb else "NO SAFE ARB"
    title = (
        "Place these two legs for a locked payout"
        if is_arb
        else "Not risk-free yet — closest strategy shown"
    )

    poly_side = str(strategy["poly_side"])
    kalshi_side = str(strategy["kalshi_side"])
    poly_price = float(strategy["poly_price"])
    kalshi_price = float(strategy["kalshi_price"])
    contracts = float(strategy["contracts"])
    guaranteed_payout = float(strategy["guaranteed_payout"])
    total_outlay = float(strategy["total_outlay"])
    net_profit = float(strategy["net_profit"])
    total_c = float(strategy["total_cost"]) * 100.0
    profit_text = _signed_money(net_profit)
    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    fee_text = f"${float(strategy['worst_case_fee']):,.2f}" if strategy.get("worst_case_fee") else "$0.00"

    edge_text = (
        f"Locked edge after {PLATFORM_FEE_PCT}% Polymarket winner fee."
        if is_arb
        else f"Needs improvement before it's risk-free (fee: {fee_text})."
    )

    st.markdown(
        f"""
        <div class="pq-arb-spotlight {cls}">
            <p class="pq-arb-spotlight-kicker">{kicker}</p>
            <p class="pq-arb-spotlight-title">{html.escape(title)}</p>
            <div class="pq-arb-action-list">
                <div class="pq-exec-step">
                    <span class="pq-exec-num">1</span>
                    <div>
                        <strong>Polymarket</strong> — Buy {html.escape(poly_side)} at {poly_price * 100:.1f}¢ ({html.escape(poly_odds)})<br>
                        <span style="color:var(--pq-text-muted);font-size:0.85rem;">{contracts:,.0f} contracts · spend ${float(strategy['poly_cash']):,.2f}</span>
                    </div>
                </div>
                <div class="pq-exec-step">
                    <span class="pq-exec-num">2</span>
                    <div>
                        <strong>Kalshi</strong> — Buy {html.escape(kalshi_side)} at {kalshi_price * 100:.1f}¢ ({html.escape(kalshi_odds)})<br>
                        <span style="color:var(--pq-text-muted);font-size:0.85rem;">{contracts:,.0f} contracts · spend ${float(strategy['kalshi_cash']):,.2f}</span>
                    </div>
                </div>
            </div>
            <p class="pq-arb-spotlight-note">
                Combined cost: <strong>{total_c:.1f}¢</strong> per $1.
                Total cash needed: <strong>${total_outlay:,.2f}</strong>.
                Guaranteed payout: <strong>${guaranteed_payout:,.2f}</strong>.
                Net profit after fees: <strong>{profit_text}</strong>.
                {html.escape(edge_text)}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_strategy_card(
    strategy: dict[str, Any],
    odds_fmt: str,
    *,
    selected: bool = False,
) -> None:
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
    net_profit = float(strategy["net_profit"])
    worst_fee = float(strategy.get("worst_case_fee", 0))
    profit_text = _signed_money(net_profit)

    poly_odds = format_odds_display(poly_price, odds_fmt)
    kalshi_odds = format_odds_display(kalshi_price, odds_fmt)
    poly_c = poly_price * 100.0
    kalshi_c = kalshi_price * 100.0
    total_c = total_cost * 100.0

    card_cls = "pq-strategy-card pq-strategy-live" if is_arb else "pq-strategy-card"
    badge_cls = "pq-strategy-badge live" if is_arb else "pq-strategy-badge dead"
    badge_txt = "Selected" if selected else ("Arb locked" if is_arb else "No lock")
    profit_class = "green" if is_arb else "red"

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
                    <span class="lbl">Net profit</span>
                    <span class="val {profit_class}">{profit_text}</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">Winner fee</span>
                    <span class="val">${worst_fee:,.2f}</span>
                </div>
                <div class="pq-metric-box">
                    <span class="lbl">ROI</span>
                    <span class="val {profit_class}">{roi:+.2f}%</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button(
        "Showing on main screen" if selected else "Show this strategy",
        key=f"arb_strategy_select_{strategy['key']}",
        use_container_width=True,
        type="primary" if selected else "secondary",
    ):
        st.session_state.arb_selected_strategy = strategy["key"]
        st.rerun()


def _auto_scan_arbs(
    poly_df: pd.DataFrame,
    kalshi_df: pd.DataFrame,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Auto-scan for cross-book arbs by fuzzy title matching."""
    from polyquant.ui.components import _tokenize_for_match

    found: list[dict[str, Any]] = []
    poly_priced = poly_df.dropna(subset=["Yes Price", "No Price"]).copy()
    kalshi_priced = filter_kalshi_tradeable(kalshi_df)

    if poly_priced.empty or kalshi_priced.empty:
        return found

    for _, p_row in poly_priced.iterrows():
        poly_yes = float(p_row["Yes Price"])
        poly_no = float(p_row["No Price"])
        poly_q = str(p_row["Question"])

        for _, k_row in kalshi_priced.iterrows():
            kalshi_yes = float(k_row["Kalshi YES Cost"])
            kalshi_no = float(k_row["Kalshi NO Cost"])
            kalshi_title = str(k_row["Title"])

            score = title_match_score(poly_q, kalshi_title)
            if score < 0.30:
                continue

            cost_a = poly_yes + kalshi_no
            cost_b = poly_no + kalshi_yes

            for cost, poly_side, kalshi_side, poly_p, kalshi_p in [
                (cost_a, "YES", "NO", poly_yes, kalshi_no),
                (cost_b, "NO", "YES", poly_no, kalshi_yes),
            ]:
                strat = build_arb_strategy(
                    f"auto_{p_row['id']}_{k_row['ticker']}_{poly_side}",
                    f"{poly_q[:60]} — Poly {poly_side} + Kalshi {kalshi_side}",
                    poly_side, poly_p, kalshi_side, kalshi_p, 100.0,
                )
                if strat["is_arb"]:
                    strat["_match_score"] = score
                    strat["_poly_q"] = poly_q
                    strat["_kalshi_title"] = kalshi_title
                    strat["_poly_id"] = p_row["id"]
                    strat["_kalshi_ticker"] = k_row["ticker"]
                    found.append(strat)

        if len(found) >= max_results:
            break

    found.sort(key=lambda s: float(s["net_profit"]), reverse=True)
    return found[:max_results]


def _render_auto_scan_results(arbs: list[dict[str, Any]], odds_fmt: str) -> None:
    if not arbs:
        st.markdown(
            '<div class="pq-card"><span class="pq-badge pq-badge-grey">'
            "No risk-free arbs found across matched pairs right now. Check back after price moves."
            "</span></div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<p class="pq-section-label">{len(arbs)} Arb{"s" if len(arbs) != 1 else ""} Found</p>',
        unsafe_allow_html=True,
    )

    for i, strat in enumerate(arbs, 1):
        net = float(strat["net_profit"])
        roi = float(strat["roi"])
        poly_side = strat["poly_side"]
        kalshi_side = strat["kalshi_side"]
        poly_price = float(strat["poly_price"])
        kalshi_price = float(strat["kalshi_price"])
        poly_odds = format_odds_display(poly_price, odds_fmt)
        kalshi_odds = format_odds_display(kalshi_price, odds_fmt)

        st.markdown(
            f"""
            <div class="pq-value-card">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <span class="pq-rank-badge">#{i}</span>
                    <span class="pq-ev-badge">+${net:.2f} profit · {roi:+.1f}% ROI</span>
                </div>
                <p class="pq-event-name" style="margin:0.5rem 0;">{html.escape(str(strat.get('_poly_q', strat['label']))[:80])}</p>
                <div class="pq-exec-step">
                    <span class="pq-exec-num">1</span>
                    <span>Polymarket: Buy {poly_side} at {poly_price*100:.1f}¢ ({html.escape(poly_odds)})</span>
                </div>
                <div class="pq-exec-step">
                    <span class="pq-exec-num">2</span>
                    <span>Kalshi: Buy {kalshi_side} at {kalshi_price*100:.1f}¢ ({html.escape(kalshi_odds)})</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_tab(tab: Any) -> None:
    with tab:
        render_tab_header(
            "Risk-Free Arbs",
            "Find guaranteed locks between Polymarket and Kalshi — fee-adjusted profit shown.",
            steps=[
                "Auto-scan finds cross-book arbs automatically by matching titles.",
                "Or manually pick a Polymarket event — Kalshi matches are auto-suggested.",
                "Plain English steps tell you exactly what to buy on each exchange.",
            ],
        )

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

        odds_fmt = get_odds_format()

        # Auto-scan section
        auto_tab, manual_tab = st.tabs(["Auto-Scan", "Manual Picker"])

        with auto_tab:
            if st.button("Scan for arbs", key="auto_scan_arbs", type="primary"):
                fetch_polymarket_markets.clear()
                fetch_kalshi_markets.clear()
                fetch_kalshi_player_props.clear()
                st.rerun()

            with st.spinner("Scanning matched pairs..."):
                auto_arbs = _auto_scan_arbs(poly_df, kalshi_df)
            _render_auto_scan_results(auto_arbs, odds_fmt)

        with manual_tab:
            st.markdown('<div class="pq-input-card">', unsafe_allow_html=True)
            c1, c2, c3 = st.columns(3)
            with c1:
                arb_stake = st.number_input(
                    "Target payout / contracts",
                    min_value=1.0,
                    value=DEFAULT_ARB_STAKE,
                    step=10.0,
                    key="arb_stake",
                    help="Equal contracts on both books. 100 = either outcome pays $100.",
                )
            with c2:
                if st.button("Refresh prices", key="refresh_arb", use_container_width=True):
                    fetch_polymarket_markets.clear()
                    fetch_kalshi_markets.clear()
                    fetch_kalshi_player_props.clear()
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

            poly_priced = poly_df.dropna(subset=["Yes Price", "No Price"]).copy()
            kalshi_priced = filter_kalshi_tradeable(kalshi_df)

            if poly_priced.empty or kalshi_priced.empty:
                st.warning("Not enough priced contracts on both books.")
                return

            poly_options = {row["id"]: row["Question"] for _, row in poly_priced.iterrows()}
            poly_prices = {
                row["id"]: book_price_hint("Polymarket", float(row["Yes Price"]), float(row["No Price"]), odds_fmt)
                for _, row in poly_priced.iterrows()
            }
            kalshi_options = {row["ticker"]: row["Title"] for _, row in kalshi_priced.iterrows()}
            kalshi_prices = {
                row["ticker"]: book_price_hint("Kalshi", float(row["Kalshi YES Cost"]), float(row["Kalshi NO Cost"]), odds_fmt)
                for _, row in kalshi_priced.iterrows()
            }

            poly_id = render_searchable_picker(
                "Polymarket Event", poly_options, "poly_selected",
                show_prices=poly_prices, collapse_after_select=True,
            )
            if not poly_id:
                return

            poly_title = poly_options[poly_id]
            suggestions = sync_kalshi_auto_suggest(poly_id, poly_title, kalshi_priced)
            render_kalshi_suggestions(suggestions, kalshi_prices)

            kalshi_ticker = render_searchable_picker(
                "Kalshi Event", kalshi_options, "kalshi_selected",
                show_prices=kalshi_prices, collapse_after_select=True,
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
                build_arb_strategy("strategy_a", "Strategy A — Poly YES + Kalshi NO", "YES", poly_yes, "NO", kalshi_no, arb_stake),
                build_arb_strategy("strategy_b", "Strategy B — Poly NO + Kalshi YES", "NO", poly_no, "YES", kalshi_yes, arb_stake),
            ]
            pair_key = f"{poly_id}::{kalshi_ticker}"
            valid_keys = {str(s["key"]) for s in strategies}
            if (
                st.session_state.get("arb_selected_pair") != pair_key
                or st.session_state.get("arb_selected_strategy") not in valid_keys
            ):
                best = best_arb_strategy(strategies)
                st.session_state.arb_selected_pair = pair_key
                st.session_state.arb_selected_strategy = best["key"]

            selected_key = st.session_state.get("arb_selected_strategy")
            selected_strategy = next(
                (s for s in strategies if s["key"] == selected_key),
                best_arb_strategy(strategies),
            )

            _render_arb_spotlight(selected_strategy, odds_fmt)
            _render_cross_book_odds(poly_row, kalshi_row, odds_fmt)

            st.markdown('<p class="pq-section-label">Compare strategies</p>', unsafe_allow_html=True)
            for strategy in strategies:
                _render_strategy_card(strategy, odds_fmt, selected=strategy["key"] == selected_strategy["key"])

            if selected_strategy["is_arb"]:
                if st.button("Save this arb basket", key="save_arb_basket", type="primary", use_container_width=True):
                    try:
                        initialize_db()
                        basket_id = save_arb_basket(selected_strategy, poly_id, kalshi_ticker)
                        st.success(f"Arb basket saved (ID: {basket_id})")
                    except Exception as e:
                        st.error(f"Could not save: {e}")

            # Saved baskets section
            try:
                initialize_db()
                summary = get_basket_summary()
                if summary["total"] > 0:
                    with st.expander(f"Saved Baskets ({summary['total']})", expanded=False):
                        render_stat_grid(
                            [
                                stat_tile("Total", str(summary["total"]), "All baskets", "neutral"),
                                stat_tile("Open", str(summary["open"]), "Unsettled", "blue"),
                                stat_tile("Settled", str(summary["settled"]), "Closed out", "neutral"),
                                stat_tile("Total P&L", f"${summary['total_pnl']:+,.2f}", "Settled baskets", "green" if summary["total_pnl"] >= 0 else "red"),
                            ],
                            cols=4,
                        )
                        baskets = list_arb_baskets()
                        for b in baskets[:10]:
                            status = "Settled" if b.get("is_settled") else "Open"
                            st.markdown(
                                f"**{b['strategy_type']}** — {status} · "
                                f"Net: ${b.get('net_profit', 0):+.2f} · "
                                f"Created: {b.get('created_at', 'N/A')}"
                            )
            except Exception:
                pass
