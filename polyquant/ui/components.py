"""Shared UI components — stat tiles, search bar, odds formatting, market matching."""
from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from typing import Any, Optional

import pandas as pd
import streamlit as st

from polyquant.config import (
    ODDS_FORMATS,
    PICKER_PAGE_SIZE,
)


def init_session() -> None:
    defaults = {
        "odds_format": "American",
        "poly_selected": None,
        "kalshi_selected": None,
        "global_search_query": "",
        "explore_category": "All",
        "explore_sports_type": "All",
        "explore_source": "Both",
        "explore_page": 0,
        "arb_poly_anchor": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def get_odds_format() -> str:
    return str(st.session_state.get("odds_format", "American")).lower()


def _price_to_american(prob: float) -> str:
    if prob <= 0 or prob >= 1:
        return "—"
    if prob >= 0.5:
        return f"{-100 * prob / (1 - prob):.0f}"
    return f"+{100 * (1 - prob) / prob:.0f}"


def format_odds_display(price: Optional[float], fmt: Optional[str] = None) -> str:
    if price is None or pd.isna(price) or price <= 0 or price >= 1:
        return "—"
    mode = (fmt or get_odds_format()).lower()
    if mode == "cents":
        return f"{price * 100:.1f}¢"
    if mode == "percentage":
        return f"{price * 100:.1f}%"
    return _price_to_american(price)


def parse_offered_odds(raw: Any, input_fmt: str) -> Optional[float]:
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
    return val


def pct_label(price: Optional[float]) -> str:
    if price is None or pd.isna(price):
        return "--%"
    return f"{price * 100:.1f}%"


def book_price_hint(
    book: str,
    yes_price: Optional[float],
    no_price: Optional[float],
    odds_fmt: str,
) -> str:
    yes_odds = format_odds_display(yes_price, odds_fmt)
    no_odds = format_odds_display(no_price, odds_fmt)
    return (
        f"{book} · YES {pct_label(yes_price)} ({yes_odds}) · "
        f"NO {pct_label(no_price)} ({no_odds})"
    )


def select_label(text: str, max_len: int = 72) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= max_len else clean[: max_len - 1] + "…"


def short_title(text: str, limit: int = 52) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rsplit(" ", 1)[0] + "…"


def render_tab_header(title: str, subtitle: str, steps: Optional[list[str]] = None) -> None:
    steps_html = ""
    if steps:
        items = "".join(f"<li>{html.escape(s)}</li>" for s in steps)
        steps_html = (
            f'<div class="pq-guide"><p class="pq-guide-label">How to use</p>'
            f'<ol class="pq-guide-steps">{items}</ol></div>'
        )
    st.markdown(
        f"""
        <div class="pq-tab-header">
            <h2 class="pq-tab-title">{html.escape(title)}</h2>
            <p class="pq-tab-subtitle">{html.escape(subtitle)}</p>
            {steps_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def stat_tile(label: str, value: str, hint: str = "", tone: str = "neutral") -> str:
    hint_html = (
        f'<span class="pq-stat-tile-hint">{html.escape(hint)}</span>' if hint else ""
    )
    return (
        f'<div class="pq-stat-tile pq-stat-{tone}">'
        f'<span class="pq-stat-tile-label">{html.escape(label)}</span>'
        f'<span class="pq-stat-tile-value">{html.escape(value)}</span>'
        f"{hint_html}</div>"
    )


def render_stat_grid(tiles: list[str], cols: int = 4) -> None:
    st.markdown(
        f'<div class="pq-stat-grid pq-stat-grid-{cols}">{"".join(tiles)}</div>',
        unsafe_allow_html=True,
    )


def render_odds_format_toggle() -> None:
    st.segmented_control(
        "Odds Display",
        options=list(ODDS_FORMATS),
        key="odds_format",
        label_visibility="collapsed",
    )


def render_global_search_bar() -> str:
    st.markdown('<div class="pq-search-hero">', unsafe_allow_html=True)
    query = st.text_input(
        "Search markets",
        key="global_search_query",
        placeholder="Search teams, players, events…",
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)
    return (query or "").strip().lower()


def render_searchable_picker(
    label: str,
    options: dict[str, str],
    session_key: str,
    *,
    show_prices: Optional[dict[str, str]] = None,
    collapse_after_select: bool = False,
) -> Optional[str]:
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
            f'{html.escape(short_title(title))}</span>'
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


# --- Title matching for cross-book arb pairing ---

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


def title_match_score(poly_title: str, kalshi_title: str) -> float:
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


def rank_kalshi_for_poly(
    poly_title: str,
    kalshi_df: pd.DataFrame,
    top_n: int = 5,
) -> list[tuple[float, str, str]]:
    ranked: list[tuple[float, str, str]] = []
    for _, row in kalshi_df.iterrows():
        ticker = str(row["ticker"])
        title = str(row["Title"])
        score = title_match_score(poly_title, title)
        if score >= 0.12:
            ranked.append((score, ticker, title))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[:top_n]


def sync_kalshi_auto_suggest(
    poly_id: str,
    poly_title: str,
    kalshi_priced: pd.DataFrame,
) -> list[tuple[float, str, str]]:
    suggestions = rank_kalshi_for_poly(poly_title, kalshi_priced)
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


def render_kalshi_suggestions(
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
                <span class="pq-suggest-title">{html.escape(short_title(title, 64))}</span>
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


def filter_kalshi_tradeable(df: pd.DataFrame) -> pd.DataFrame:
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
