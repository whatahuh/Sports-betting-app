"""
POLY-QUANT v2
=============
Sports betting intelligence terminal — Polymarket + Kalshi.

Run with:
    streamlit run app.py
"""
from __future__ import annotations

import html

import streamlit as st

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from polyquant.config import APP_BUILD, CACHE_TTL
from polyquant.db.schema import initialize_db
from polyquant.ui.css import inject_global_css
from polyquant.ui.components import (
    init_session,
    render_global_search_bar,
    render_odds_format_toggle,
)
from polyquant.ui import tab_value_plays, tab_audit, tab_hype, tab_arbs


st.set_page_config(
    page_title=f"POLY-QUANT · {APP_BUILD}",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

inject_global_css()
init_session()
initialize_db()

st.markdown(
    f"""
    <div class="pq-app-header">
        <div class="pq-app-header-top">
            <div class="pq-brand">
                <div class="pq-brand-mark">PQ</div>
                <div>
                    <div class="pq-brand-name">POLY-QUANT</div>
                    <div class="pq-brand-tagline">Sports betting intelligence · Polymarket + Kalshi</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:0.65rem;flex-wrap:wrap;">
                <span class="pq-live-pill"><span class="pq-live-dot"></span> Live Data</span>
                <span class="pq-version-chip">{html.escape(APP_BUILD)}</span>
            </div>
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


def main() -> None:
    tab_plays, tab_audit_tab, tab_hype_tab, tab_arb_tab = st.tabs([
        "Top Value Plays",
        "Audit My Bet",
        "Hype vs Reality",
        "Risk-Free Arbs",
    ])

    tab_value_plays.render_tab(tab_plays)
    tab_audit.render_tab(tab_audit_tab)
    tab_hype.render_tab(tab_hype_tab)
    tab_arbs.render_tab(tab_arb_tab)

    st.markdown(
        f'<div class="pq-footer">POLY-QUANT · Build <strong>{html.escape(APP_BUILD)}</strong> · '
        f"Data refreshes every {CACHE_TTL}s</div>",
        unsafe_allow_html=True,
    )


main()
