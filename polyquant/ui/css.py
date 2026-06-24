"""Global CSS injection for the Poly-Quant terminal UI."""
from __future__ import annotations

import streamlit as st


def inject_global_css() -> None:
    """Inject all CSS custom properties and component styles."""
    st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;600&display=swap');

            :root {
                --pq-bg: #07080c;
                --pq-bg-elevated: #0e1018;
                --pq-surface: #13161f;
                --pq-surface-hover: #1a1e2a;
                --pq-border: rgba(255,255,255,0.08);
                --pq-border-strong: rgba(255,255,255,0.14);
                --pq-text: #f4f5f7;
                --pq-text-muted: #9aa3b2;
                --pq-text-dim: #6b7280;
                --pq-accent: #6c8cff;
                --pq-accent-glow: rgba(108,140,255,0.25);
                --pq-green: #34d399;
                --pq-green-glow: rgba(52,211,153,0.2);
                --pq-red: #f87171;
                --pq-amber: #fbbf24;
                --pq-radius: 14px;
                --pq-radius-sm: 10px;
                --pq-shadow: 0 4px 24px rgba(0,0,0,0.35);
                --pq-font: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
                --pq-mono: 'JetBrains Mono', ui-monospace, monospace;
            }

            #MainMenu, header, footer, .stDeployButton {visibility: hidden; display: none;}

            .stApp {
                background: var(--pq-bg);
                background-image:
                    radial-gradient(ellipse 80% 50% at 50% -20%, rgba(108,140,255,0.12), transparent),
                    radial-gradient(ellipse 60% 40% at 100% 0%, rgba(52,211,153,0.06), transparent);
                color: var(--pq-text);
                font-family: var(--pq-font);
            }

            .block-container {
                padding: 0.75rem 1.25rem 2.5rem;
                max-width: 1200px;
            }

            /* -- App header -- */
            .pq-app-header {
                background: linear-gradient(135deg, var(--pq-surface) 0%, var(--pq-bg-elevated) 100%);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1.1rem 1.35rem;
                margin-bottom: 1rem;
                box-shadow: var(--pq-shadow);
            }
            .pq-app-header-top {
                display: flex;
                align-items: center;
                justify-content: space-between;
                flex-wrap: wrap;
                gap: 0.75rem;
                margin-bottom: 0.85rem;
            }
            .pq-brand {
                display: flex;
                align-items: center;
                gap: 0.65rem;
            }
            .pq-brand-mark {
                width: 36px;
                height: 36px;
                border-radius: 10px;
                background: linear-gradient(135deg, var(--pq-accent), #4f6ef7);
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 1rem;
                font-weight: 900;
                color: #fff;
                box-shadow: 0 0 20px var(--pq-accent-glow);
            }
            .pq-brand-name {
                font-size: 1.25rem;
                font-weight: 900;
                letter-spacing: -0.03em;
                color: var(--pq-text);
                line-height: 1.1;
            }
            .pq-brand-tagline {
                font-size: 0.72rem;
                font-weight: 500;
                color: var(--pq-text-muted);
                margin-top: 0.1rem;
            }
            .pq-live-pill {
                display: inline-flex;
                align-items: center;
                gap: 0.4rem;
                background: rgba(52,211,153,0.12);
                border: 1px solid rgba(52,211,153,0.35);
                color: var(--pq-green);
                font-size: 0.72rem;
                font-weight: 700;
                padding: 0.35rem 0.75rem;
                border-radius: 999px;
                letter-spacing: 0.02em;
            }
            .pq-live-dot {
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: var(--pq-green);
                box-shadow: 0 0 8px var(--pq-green);
                animation: pq-pulse 2s ease-in-out infinite;
            }
            @keyframes pq-pulse {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.6; transform: scale(0.85); }
            }
            .pq-version-chip {
                font-size: 0.68rem;
                font-weight: 600;
                color: var(--pq-text-dim);
                font-family: var(--pq-mono);
            }

            /* Tabs */
            .stTabs [data-baseweb="tab-list"] {
                gap: 4px;
                background: var(--pq-bg-elevated);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 4px;
                margin-bottom: 1.25rem;
            }
            .stTabs [data-baseweb="tab"] {
                background: transparent;
                color: var(--pq-text-muted);
                font-weight: 600;
                font-size: 0.8rem;
                padding: 10px 16px;
                border-radius: 8px;
                border: none;
                transition: all 0.15s ease;
            }
            .stTabs [data-baseweb="tab"]:hover {
                color: var(--pq-text);
                background: var(--pq-surface-hover);
            }
            .stTabs [aria-selected="true"] {
                color: var(--pq-text) !important;
                background: var(--pq-surface) !important;
                border: 1px solid var(--pq-border-strong) !important;
                box-shadow: 0 2px 8px rgba(0,0,0,0.2) !important;
            }
            .stTabs [data-baseweb="tab-highlight"],
            .stTabs [data-baseweb="tab-border"] {
                display: none !important;
            }

            /* Tab headers & guides */
            .pq-tab-header {
                margin-bottom: 1.25rem;
            }
            .pq-tab-title {
                font-size: 1.5rem;
                font-weight: 900;
                letter-spacing: -0.03em;
                color: var(--pq-text);
                margin: 0 0 0.35rem;
                line-height: 1.2;
            }
            .pq-tab-subtitle {
                font-size: 0.9rem;
                color: var(--pq-text-muted);
                margin: 0 0 0.85rem;
                line-height: 1.5;
                max-width: 640px;
            }
            .pq-guide {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-left: 3px solid var(--pq-accent);
                border-radius: var(--pq-radius-sm);
                padding: 0.85rem 1rem;
                margin-top: 0.5rem;
            }
            .pq-guide-label {
                font-size: 0.68rem;
                font-weight: 800;
                color: var(--pq-accent);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin: 0 0 0.45rem;
            }
            .pq-guide-steps {
                margin: 0;
                padding-left: 1.15rem;
                color: var(--pq-text-muted);
                font-size: 0.82rem;
                line-height: 1.55;
            }
            .pq-guide-steps li { margin-bottom: 0.25rem; }

            /* Stat tiles */
            .pq-stat-grid {
                display: grid;
                gap: 0.65rem;
                margin-bottom: 0.85rem;
            }
            .pq-stat-grid-4 { grid-template-columns: repeat(4, 1fr); }
            .pq-stat-grid-3 { grid-template-columns: repeat(3, 1fr); }
            @media (max-width: 900px) {
                .pq-stat-grid-4 { grid-template-columns: repeat(2, 1fr); }
            }
            @media (max-width: 520px) {
                .pq-stat-grid-4, .pq-stat-grid-3 { grid-template-columns: 1fr; }
            }
            .pq-stat-tile {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 0.9rem 1rem;
                transition: border-color 0.15s ease, transform 0.15s ease;
            }
            .pq-stat-tile:hover {
                border-color: var(--pq-border-strong);
                transform: translateY(-1px);
            }
            .pq-stat-green { border-left: 3px solid var(--pq-green); }
            .pq-stat-blue { border-left: 3px solid var(--pq-accent); }
            .pq-stat-red { border-left: 3px solid var(--pq-red); }
            .pq-stat-amber { border-left: 3px solid var(--pq-amber); }
            .pq-stat-amber .pq-stat-tile-value { color: var(--pq-amber); }
            .pq-stat-neutral { border-left: 3px solid var(--pq-border-strong); }
            .pq-stat-tile-label {
                display: block;
                font-size: 0.68rem;
                font-weight: 700;
                color: var(--pq-text-dim);
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.35rem;
            }
            .pq-stat-tile-value {
                display: block;
                font-size: 1.45rem;
                font-weight: 900;
                color: var(--pq-text);
                letter-spacing: -0.02em;
                line-height: 1.1;
                font-family: var(--pq-mono);
            }
            .pq-stat-green .pq-stat-tile-value { color: var(--pq-green); }
            .pq-stat-blue .pq-stat-tile-value { color: var(--pq-accent); }
            .pq-stat-red .pq-stat-tile-value { color: var(--pq-red); }
            .pq-stat-tile-hint {
                display: block;
                font-size: 0.72rem;
                color: var(--pq-text-muted);
                margin-top: 0.3rem;
            }

            /* Insight & action cards */
            .pq-insight-banner {
                display: flex;
                align-items: flex-start;
                gap: 0.65rem;
                background: linear-gradient(90deg, rgba(52,211,153,0.12), rgba(52,211,153,0.04));
                border: 1px solid rgba(52,211,153,0.3);
                border-radius: var(--pq-radius-sm);
                padding: 0.85rem 1rem;
                margin: 0.5rem 0 1rem;
                font-size: 0.88rem;
                color: var(--pq-text-muted);
                line-height: 1.5;
            }
            .pq-insight-banner strong { color: var(--pq-text); }
            .pq-insight-muted {
                background: var(--pq-surface);
                border-color: var(--pq-border);
            }
            .pq-insight-icon { font-size: 1.1rem; flex-shrink: 0; }
            .pq-action-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 1rem;
                height: 100%;
                transition: border-color 0.15s, box-shadow 0.15s;
            }
            .pq-action-card:hover {
                border-color: var(--pq-accent);
                box-shadow: 0 0 20px var(--pq-accent-glow);
            }
            .pq-action-icon { font-size: 1.35rem; display: block; margin-bottom: 0.45rem; }
            .pq-action-title {
                font-size: 0.92rem;
                font-weight: 800;
                color: var(--pq-text);
                margin: 0 0 0.3rem;
            }
            .pq-action-desc {
                font-size: 0.78rem;
                color: var(--pq-text-muted);
                margin: 0;
                line-height: 1.45;
            }
            .pq-workflow {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 0.55rem;
                margin-bottom: 1rem;
            }
            @media (max-width: 640px) {
                .pq-workflow { grid-template-columns: 1fr; }
            }
            .pq-workflow-step {
                display: flex;
                gap: 0.65rem;
                align-items: flex-start;
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 0.75rem 0.9rem;
                font-size: 0.82rem;
                color: var(--pq-text-muted);
                line-height: 1.45;
            }
            .pq-workflow-step strong { color: var(--pq-text); }
            .pq-workflow-num {
                font-size: 0.68rem;
                font-weight: 900;
                color: var(--pq-accent);
                background: rgba(108,140,255,0.12);
                border-radius: 6px;
                padding: 0.25rem 0.45rem;
                white-space: nowrap;
                flex-shrink: 0;
            }
            .pq-connect-banner {
                background: linear-gradient(90deg, rgba(108,140,255,0.1), transparent);
                border: 1px dashed rgba(108,140,255,0.35);
                border-radius: var(--pq-radius-sm);
                padding: 0.85rem 1rem;
                font-size: 0.85rem;
                color: var(--pq-text-muted);
                margin-top: 0.5rem;
            }
            .pq-connect-banner strong { color: var(--pq-accent); }

            /* Cards */
            .pq-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.9rem 1rem;
                margin-bottom: 0.55rem;
            }
            .pq-card-compound {
                border-color: #238636;
                background: linear-gradient(135deg, rgba(35,134,54,0.15) 0%, var(--pq-surface) 60%);
                box-shadow: 0 0 20px rgba(63,185,80,0.12);
            }
            .pq-card-title {
                font-size: 0.92rem;
                font-weight: 700;
                color: var(--pq-text);
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
                background: rgba(108,140,255,0.15);
                color: var(--pq-accent);
                border: 1px solid rgba(108,140,255,0.35);
            }
            .pq-badge-grey {
                background: var(--pq-surface-hover);
                color: var(--pq-text-muted);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-badge-red {
                background: rgba(248,81,73,0.15);
                color: var(--pq-red);
                border: 1px solid rgba(248,81,73,0.35);
            }
            .pq-stat {
                font-size: 0.78rem;
                color: var(--pq-text-muted);
            }
            .pq-stat strong {
                color: var(--pq-text);
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
                color: var(--pq-text);
                line-height: 1.5;
            }
            .pq-verdict-pass {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border-strong);
                border-radius: 14px;
                padding: 1.25rem 1.35rem;
                margin-top: 1rem;
            }
            .pq-verdict-pass h2 {
                margin: 0 0 0.35rem;
                font-size: 1.2rem;
                font-weight: 800;
                color: var(--pq-text-muted);
            }
            .pq-verdict-pass p {
                margin: 0;
                font-size: 0.88rem;
                color: var(--pq-text-dim);
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
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.85rem;
                text-align: center;
            }
            .pq-split-side .venue {
                font-size: 0.68rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.35rem;
            }
            .pq-split-side .leg {
                font-size: 1rem;
                font-weight: 800;
                color: var(--pq-accent);
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
                color: var(--pq-text);
                font-size: 0.9rem;
            }

            /* Warning banner */
            .pq-trap-banner {
                background: linear-gradient(135deg, rgba(248,81,73,0.2), rgba(139,69,19,0.1));
                border: 2px solid var(--pq-red);
                border-radius: 12px;
                padding: 1.1rem 1.2rem;
                margin-top: 0.75rem;
            }
            .pq-trap-banner h3 {
                margin: 0 0 0.4rem;
                color: var(--pq-red);
                font-size: 1rem;
                font-weight: 800;
            }
            .pq-trap-banner p {
                margin: 0;
                color: var(--pq-text);
                font-size: 0.88rem;
                line-height: 1.45;
            }

            /* Input card */
            .pq-input-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.85rem 1rem 0.25rem;
                margin-bottom: 0.75rem;
            }

            /* Streamlit widgets */
            [data-testid="stMetric"] {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 0.75rem 0.9rem;
            }
            [data-testid="stMetricLabel"] {
                font-size: 0.72rem !important;
                font-weight: 700 !important;
                color: var(--pq-text-dim) !important;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            [data-testid="stMetricValue"] {
                font-family: var(--pq-mono) !important;
                font-weight: 800 !important;
            }
            [data-testid="stDataFrame"] {
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
            }
            .stSlider label, .stNumberInput label, .stSelectbox label {
                font-weight: 600 !important;
                font-size: 0.82rem !important;
            }
            hr {
                border-color: var(--pq-border);
                margin: 0.75rem 0;
            }

            /* Section labels & picker */
            .pq-section-label {
                font-size: 0.72rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin: 0.65rem 0 0.35rem;
            }
            .pq-pick-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 10px;
                padding: 0.55rem 0.75rem;
                margin-bottom: 0.25rem;
            }
            .pq-pick-selected {
                border-color: var(--pq-accent);
                background: rgba(108,140,255,0.08);
            }
            .pq-pick-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 600;
                color: var(--pq-text);
                line-height: 1.35;
            }
            .pq-pick-meta {
                display: block;
                font-size: 0.72rem;
                color: var(--pq-accent);
                font-weight: 700;
                margin-top: 0.15rem;
            }
            .pq-page-indicator {
                text-align: center;
                font-size: 0.75rem;
                color: var(--pq-text-muted);
                margin: 0.35rem 0 0;
            }
            .pq-selected-banner {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 10px;
                padding: 0.65rem 0.8rem;
                font-size: 0.78rem;
                color: var(--pq-text);
                line-height: 1.4;
                margin: 0.5rem 0 0.75rem;
            }
            .pq-odds-bar {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
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
                background: var(--pq-surface-hover) !important;
                border: 1px solid var(--pq-border-strong) !important;
                color: var(--pq-text) !important;
            }
            .stButton > button[kind="primary"] {
                background: #1f6feb !important;
                border: 1px solid #388bfd !important;
            }

            /* Segmented control polish */
            [data-testid="stSegmentedControl"] {
                background: var(--pq-bg);
                border-radius: 10px;
                padding: 3px;
            }

            /* Pikkit-style explore feed */
            .pq-search-hero {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 0.5rem 0.75rem;
                margin-bottom: 0.75rem;
            }
            .pq-search-hero input {
                font-size: 0.9rem !important;
            }
            .pq-feed-row {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 0.75rem 0.85rem;
                margin-bottom: 0.45rem;
            }
            .pq-feed-meta {
                display: block;
                font-size: 0.65rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.25rem;
            }
            .pq-feed-title {
                display: block;
                font-size: 0.88rem;
                font-weight: 700;
                color: var(--pq-text);
                line-height: 1.35;
            }
            .pq-feed-event {
                display: block;
                font-size: 0.72rem;
                color: var(--pq-text-dim);
                margin-top: 0.2rem;
            }
            .pq-odd-pill {
                display: block;
                text-align: center;
                padding: 0.55rem 0.35rem;
                border-radius: 10px;
                font-weight: 800;
                font-size: 0.95rem;
            }
            .pq-odd-yes {
                background: rgba(108,140,255,0.12);
                color: var(--pq-accent);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-odd-no {
                background: var(--pq-surface-hover);
                color: var(--pq-text);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-nav-scroll .stPills {
                overflow-x: auto;
            }

            /* Phase 1 -- tactile value cards */
            .pq-value-card-hot {
                border-color: #238636;
                box-shadow: 0 0 18px rgba(63,185,80,0.15);
            }
            .pq-event-name {
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--pq-text);
                margin: 0 0 0.65rem;
                line-height: 1.35;
            }
            .pq-cta-pill {
                display: inline-block;
                background: linear-gradient(90deg, #1f6feb, #388bfd);
                color: #fff;
                font-weight: 800;
                font-size: 0.82rem;
                padding: 0.45rem 0.85rem;
                border-radius: 999px;
                margin-bottom: 0.55rem;
                letter-spacing: 0.02em;
            }
            .pq-ev-badge {
                display: inline-block;
                background: rgba(63,185,80,0.25);
                color: #3fb950;
                border: 1px solid #3fb950;
                font-weight: 800;
                font-size: 0.8rem;
                padding: 0.3rem 0.65rem;
                border-radius: 8px;
            }
            .pq-metric-row {
                display: flex;
                gap: 1.25rem;
                flex-wrap: wrap;
                font-size: 0.78rem;
                color: var(--pq-text-muted);
            }
            .pq-metric-row strong { color: var(--pq-text); }

            /* Full-width audit banner */
            .pq-banner-play {
                background: linear-gradient(90deg, rgba(63,185,80,0.35), rgba(35,134,54,0.15));
                border: 2px solid #3fb950;
                border-radius: 12px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.45rem;
                font-weight: 900;
                color: #3fb950;
                margin-top: 1rem;
                letter-spacing: 0.04em;
            }
            .pq-banner-pass {
                background: rgba(88,28,28,0.35);
                border: 2px solid #6e3630;
                border-radius: 12px;
                padding: 1.4rem;
                text-align: center;
                font-size: 1.35rem;
                font-weight: 900;
                color: var(--pq-text-muted);
                margin-top: 1rem;
            }

            /* Hype vs Reality */
            .pq-hype-col {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 12px;
                padding: 1rem;
                text-align: center;
            }
            .pq-hype-val {
                font-size: 2rem;
                font-weight: 900;
                color: var(--pq-text);
            }
            .pq-bubble-badge {
                background: linear-gradient(90deg, rgba(255,140,0,0.35), rgba(255,69,0,0.2));
                border: 2px solid #ff8c00;
                color: #ffb347;
                font-weight: 900;
                font-size: 0.95rem;
                padding: 1rem 1.1rem;
                border-radius: 12px;
                text-align: center;
                margin-top: 0.85rem;
                box-shadow: 0 0 20px rgba(255,140,0,0.2);
            }

            /* Arb recipe */
            .pq-recipe {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 1rem 1.15rem;
                margin: 0.5rem 0;
            }
            .pq-recipe-step {
                font-size: 0.92rem;
                color: var(--pq-text);
                margin: 0.45rem 0;
                line-height: 1.5;
            }
            .pq-recipe-step strong { color: var(--pq-accent); }
            .pq-lock-banner {
                background: linear-gradient(90deg, rgba(63,185,80,0.3), rgba(35,134,54,0.12));
                border: 2px solid #3fb950;
                border-radius: 12px;
                padding: 1rem;
                text-align: center;
                font-size: 1.1rem;
                font-weight: 800;
                color: #3fb950;
                margin-top: 0.75rem;
            }

            /* Cross-book arb comparison */
            .pq-arb-compare {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 1rem 1.1rem;
                margin: 0.75rem 0 1rem;
            }
            .pq-arb-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 0.65rem;
            }
            @media (max-width: 640px) {
                .pq-arb-grid { grid-template-columns: 1fr; }
            }
            .pq-book-card {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 12px;
                padding: 0.85rem;
            }
            .pq-book-header {
                font-size: 0.68rem;
                font-weight: 800;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.35rem;
            }
            .pq-book-title {
                font-size: 0.82rem;
                font-weight: 700;
                color: var(--pq-text);
                line-height: 1.35;
                margin-bottom: 0.65rem;
                min-height: 2.2rem;
            }
            .pq-odd-row {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 0.45rem 0.55rem;
                border-radius: 8px;
                margin-bottom: 0.35rem;
                font-size: 0.8rem;
                font-weight: 700;
            }
            .pq-odd-row.yes {
                background: rgba(108,140,255,0.12);
                border: 1px solid rgba(108,140,255,0.35);
                color: var(--pq-accent);
            }
            .pq-odd-row.no {
                background: var(--pq-surface-hover);
                border: 1px solid var(--pq-border-strong);
                color: var(--pq-text);
            }
            .pq-odd-row .pq-odd-val {
                font-weight: 800;
                color: var(--pq-text);
                font-size: 0.78rem;
            }
            .pq-strategy-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: 14px;
                padding: 1rem 1.1rem;
                margin: 0.65rem 0;
            }
            .pq-strategy-card.pq-strategy-live {
                border-color: #3fb950;
                box-shadow: 0 0 20px rgba(63,185,80,0.15);
            }
            .pq-strategy-head {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 0.65rem;
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .pq-strategy-title {
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--pq-text);
                margin: 0;
            }
            .pq-strategy-badge {
                font-size: 0.68rem;
                font-weight: 800;
                padding: 0.25rem 0.55rem;
                border-radius: 999px;
                text-transform: uppercase;
                letter-spacing: 0.04em;
            }
            .pq-strategy-badge.live {
                background: rgba(63,185,80,0.22);
                color: #3fb950;
                border: 1px solid #3fb950;
            }
            .pq-strategy-badge.dead {
                background: var(--pq-surface-hover);
                color: var(--pq-text-muted);
                border: 1px solid var(--pq-border-strong);
            }
            .pq-strategy-metrics {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 0.45rem;
                margin-top: 0.65rem;
            }
            @media (max-width: 480px) {
                .pq-strategy-metrics { grid-template-columns: 1fr; }
            }
            .pq-metric-box {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 10px;
                padding: 0.55rem 0.65rem;
                text-align: center;
            }
            .pq-metric-box .lbl {
                display: block;
                font-size: 0.62rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .pq-metric-box .val {
                display: block;
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--pq-text);
                margin-top: 0.15rem;
            }
            .pq-metric-box .val.green { color: #3fb950; }
            .pq-metric-box .val.red { color: var(--pq-red); }
            .pq-arb-detail {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 12px;
                padding: 0.8rem;
                margin-top: 0.7rem;
            }
            .pq-arb-detail-title {
                margin: 0 0 0.5rem;
                color: var(--pq-text);
                font-size: 0.78rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.06em;
            }
            .pq-arb-ticket-row {
                display: grid;
                grid-template-columns: 1.25fr 0.75fr 0.8fr 0.9fr;
                gap: 0.45rem;
                align-items: center;
                padding: 0.5rem 0;
                border-top: 1px solid var(--pq-border);
                font-size: 0.78rem;
                color: var(--pq-text);
            }
            .pq-arb-ticket-row.header {
                border-top: 0;
                padding-top: 0;
                color: var(--pq-text-muted);
                font-size: 0.66rem;
                font-weight: 800;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .pq-arb-ticket-row strong {
                color: var(--pq-text);
                font-weight: 800;
            }
            .pq-arb-ticket-row .cash {
                color: var(--pq-accent);
                font-weight: 800;
                text-align: right;
            }
            .pq-arb-explain {
                margin: 0.65rem 0 0;
                color: var(--pq-text);
                font-size: 0.82rem;
                line-height: 1.48;
            }
            .pq-arb-explain strong { color: var(--pq-text); }
            .pq-arb-warning {
                background: rgba(248,81,73,0.12);
                border: 1px solid rgba(248,81,73,0.45);
                border-radius: 10px;
                color: #ffb3ad;
                font-size: 0.8rem;
                line-height: 1.45;
                margin-top: 0.7rem;
                padding: 0.7rem 0.75rem;
            }
            .pq-arb-spotlight {
                background: linear-gradient(180deg, rgba(108,140,255,0.18), rgba(13,17,23,0.96));
                border: 2px solid var(--pq-accent);
                border-radius: 16px;
                box-shadow: 0 0 26px rgba(108,140,255,0.18);
                margin: 0.75rem 0 1rem;
                padding: 1rem;
            }
            .pq-arb-spotlight.live {
                background: linear-gradient(180deg, rgba(63,185,80,0.22), rgba(13,17,23,0.96));
                border-color: #3fb950;
                box-shadow: 0 0 28px rgba(63,185,80,0.22);
            }
            .pq-arb-spotlight.dead {
                background: linear-gradient(180deg, rgba(248,81,73,0.16), rgba(13,17,23,0.96));
                border-color: var(--pq-red);
                box-shadow: 0 0 24px rgba(248,81,73,0.15);
            }
            .pq-arb-spotlight-kicker {
                color: var(--pq-text-muted);
                font-size: 0.68rem;
                font-weight: 900;
                letter-spacing: 0.08em;
                margin: 0 0 0.25rem;
                text-transform: uppercase;
            }
            .pq-arb-spotlight-title {
                color: var(--pq-text);
                font-size: 1.1rem;
                font-weight: 900;
                letter-spacing: -0.02em;
                line-height: 1.2;
                margin: 0 0 0.7rem;
            }
            .pq-arb-action-list {
                display: grid;
                gap: 0.5rem;
                margin: 0.7rem 0;
            }
            .pq-arb-action {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 12px;
                padding: 0.75rem;
            }
            .pq-arb-action .step {
                color: var(--pq-text-muted);
                display: block;
                font-size: 0.66rem;
                font-weight: 900;
                letter-spacing: 0.06em;
                text-transform: uppercase;
            }
            .pq-arb-action .take {
                color: var(--pq-text);
                display: block;
                font-size: 0.94rem;
                font-weight: 900;
                margin-top: 0.15rem;
            }
            .pq-arb-action .meta {
                color: var(--pq-accent);
                display: block;
                font-size: 0.78rem;
                font-weight: 700;
                margin-top: 0.2rem;
            }
            .pq-arb-spotlight-note {
                color: var(--pq-text);
                font-size: 0.82rem;
                line-height: 1.5;
                margin: 0.65rem 0 0;
            }
            .pq-arb-spotlight-note strong { color: var(--pq-text); }
            @media (max-width: 480px) {
                .pq-arb-ticket-row {
                    grid-template-columns: 1fr 0.62fr;
                    gap: 0.28rem 0.45rem;
                }
                .pq-arb-ticket-row.header { display: none; }
                .pq-arb-ticket-row .cash { text-align: left; }
            }

            /* Kalshi auto-suggest */
            .pq-suggest-card {
                background: var(--pq-bg);
                border: 1px solid var(--pq-border-strong);
                border-radius: 12px;
                padding: 0.7rem 0.85rem;
                margin-bottom: 0.35rem;
            }
            .pq-suggest-score {
                display: inline-block;
                font-size: 0.65rem;
                font-weight: 800;
                color: var(--pq-accent);
                background: rgba(108,140,255,0.12);
                border: 1px solid rgba(108,140,255,0.35);
                border-radius: 999px;
                padding: 0.15rem 0.45rem;
                margin-bottom: 0.35rem;
            }
            .pq-suggest-title {
                display: block;
                font-size: 0.84rem;
                font-weight: 700;
                color: var(--pq-text);
                line-height: 1.35;
            }
            .pq-suggest-meta {
                display: block;
                font-size: 0.72rem;
                color: var(--pq-accent);
                font-weight: 600;
                margin-top: 0.2rem;
            }
            .pq-build-tag {
                color: var(--pq-accent);
                font-weight: 700;
            }

            /* Pikkit-style performance calendar */
            .pq-perf-calendar {
                background: var(--pq-bg-elevated);
                border: 1px solid var(--pq-border);
                border-radius: 10px;
                padding: 0.85rem 0.95rem 1rem;
                margin: 0.65rem 0 1rem;
            }
            .pq-perf-cal-header {
                display: flex;
                justify-content: space-between;
                align-items: baseline;
                margin-bottom: 0.65rem;
                flex-wrap: wrap;
                gap: 0.35rem;
            }
            .pq-perf-cal-title {
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--pq-text);
                letter-spacing: -0.02em;
            }
            .pq-perf-cal-sub {
                font-size: 0.72rem;
                font-weight: 600;
                color: var(--pq-text-muted);
            }
            .pq-perf-cal-month-pnl {
                font-size: 0.82rem;
                font-weight: 800;
            }
            .pq-perf-cal-month-pnl.pos { color: #3fb950; }
            .pq-perf-cal-month-pnl.neg { color: var(--pq-red); }
            .pq-perf-cal-month-pnl.flat { color: var(--pq-text-muted); }
            .pq-perf-cal-grid {
                display: grid;
                grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 6px;
            }
            .pq-perf-cal-head {
                text-align: center;
                font-size: 0.62rem;
                font-weight: 800;
                color: var(--pq-text-dim);
                text-transform: uppercase;
                letter-spacing: 0.06em;
                padding: 0.2rem 0 0.35rem;
            }
            .pq-perf-cal-cell {
                min-height: 58px;
                border-radius: 8px;
                border: 1px solid var(--pq-border);
                background: var(--pq-bg);
                padding: 0.35rem 0.3rem 0.3rem;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                align-items: stretch;
            }
            .pq-perf-cal-cell.pq-perf-empty {
                background: transparent;
                border-color: transparent;
                min-height: 0;
                padding: 0;
            }
            .pq-perf-cal-cell.pq-perf-today {
                box-shadow: 0 0 0 2px var(--pq-accent);
            }
            .pq-perf-cal-cell.pq-perf-win {
                background: rgba(63,185,80,0.18);
                border-color: rgba(63,185,80,0.45);
            }
            .pq-perf-cal-cell.pq-perf-loss {
                background: rgba(248,81,73,0.14);
                border-color: rgba(248,81,73,0.4);
            }
            .pq-perf-cal-cell.pq-perf-flat {
                background: var(--pq-surface);
                border-color: var(--pq-border-strong);
            }
            .pq-perf-cal-day {
                font-size: 0.62rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                line-height: 1;
            }
            .pq-perf-cal-pnl {
                font-size: 0.72rem;
                font-weight: 800;
                text-align: center;
                line-height: 1.1;
                margin-top: 0.15rem;
            }
            .pq-perf-cal-pnl.pos { color: #3fb950; }
            .pq-perf-cal-pnl.neg { color: var(--pq-red); }
            .pq-perf-cal-pnl.flat { color: var(--pq-text); }
            .pq-perf-cal-count {
                font-size: 0.58rem;
                font-weight: 600;
                color: var(--pq-text-dim);
                text-align: center;
                margin-top: 0.1rem;
            }

            /* Ledger calendar flexbox (legacy) */
            .pq-calendar-wrap { margin: 0.75rem 0 1rem; }
            .pq-cal-grid {
                display: flex;
                flex-wrap: wrap;
                gap: 4px;
            }
            .pq-cal-head {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                text-align: center;
                font-size: 0.65rem;
                font-weight: 700;
                color: var(--pq-text-muted);
                padding: 0.25rem 0;
            }
            .pq-cal-cell {
                flex: 1 0 calc(14.28% - 4px);
                min-width: 0;
                aspect-ratio: 1;
                border-radius: 8px;
                border: 1px solid var(--pq-border);
                position: relative;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .pq-cal-day {
                position: absolute;
                top: 4px;
                left: 6px;
                font-size: 0.62rem;
                color: var(--pq-text-muted);
                font-weight: 600;
            }
            .pq-cal-neutral { background: var(--pq-surface); }
            .pq-cal-win { background: rgba(63,185,80,0.22); border-color: rgba(63,185,80,0.4); }
            .pq-cal-loss { background: rgba(248,81,73,0.18); border-color: rgba(248,81,73,0.35); }
            .pq-cal-pnl { font-size: 0.72rem; font-weight: 800; }
            .pq-cal-pnl.pos { color: #3fb950; }
            .pq-cal-pnl.neg { color: var(--pq-red); }
            .pq-cal-dash { color: var(--pq-text-dim); font-size: 0.85rem; }

            /* Elite value plays (SOP) */
            .pq-value-card-elite {
                border: 2px solid #3fb950;
                box-shadow: 0 0 22px rgba(63,185,80,0.28);
            }
            .pq-rank-badge {
                display: inline-block; background: rgba(63,185,80,0.2);
                color: #3fb950; border: 1px solid #3fb950;
                font-weight: 800; font-size: 0.72rem;
                padding: 0.28rem 0.6rem; border-radius: 999px;
                margin-bottom: 0.5rem; letter-spacing: 0.03em;
            }
            .pq-rank-badge-elite {
                background: rgba(63,185,80,0.35); font-size: 0.78rem;
            }

            /* Compact explore feed -- single row on mobile */
            .pq-feed-compact {
                display: flex; align-items: center; justify-content: space-between;
                gap: 0.65rem; flex-wrap: wrap;
            }
            .pq-feed-body { flex: 1 1 200px; min-width: 0; }
            .pq-feed-odds {
                display: flex; gap: 0.35rem; flex-shrink: 0;
            }
            .pq-odd-pill.sm {
                padding: 0.35rem 0.5rem; font-size: 0.78rem;
                border-radius: 8px; white-space: nowrap;
            }

            /* Scrollable tabs on mobile */
            .stTabs [data-baseweb="tab-list"] {
                flex-wrap: nowrap !important;
                overflow-x: auto !important;
                -webkit-overflow-scrolling: touch;
            }
            .stTabs [data-baseweb="tab"] {
                white-space: nowrap !important;
                flex-shrink: 0 !important;
            }

            .block-container { max-width: 1200px; padding-bottom: 2.5rem; }

            /* Toolbar row */
            .pq-toolbar {
                display: flex;
                gap: 0.75rem;
                align-items: stretch;
                margin-bottom: 0.25rem;
            }
            .pq-toolbar-search { flex: 3; min-width: 0; }
            .pq-toolbar-odds { flex: 1; min-width: 140px; }

            /* ====================================================================
               NEW CLASSES -- Redesigned UI components
               ==================================================================== */

            /* PLAYABLE/AVOID verdict badges */
            .pq-verdict-playable {
                background: linear-gradient(135deg, rgba(52,211,153,0.25) 0%, rgba(52,211,153,0.08) 100%);
                border: 2px solid var(--pq-green);
                border-radius: 16px;
                padding: 1.5rem;
                text-align: center;
                margin: 1rem 0;
            }
            .pq-verdict-playable h2 {
                color: var(--pq-green);
                font-size: 2rem;
                font-weight: 900;
                margin: 0 0 0.5rem;
            }
            .pq-verdict-playable p {
                color: var(--pq-text-muted);
                font-size: 1rem;
                margin: 0;
            }
            .pq-verdict-avoid {
                background: linear-gradient(135deg, rgba(248,113,113,0.2) 0%, rgba(248,113,113,0.05) 100%);
                border: 2px solid var(--pq-red);
                border-radius: 16px;
                padding: 1.5rem;
                text-align: center;
                margin: 1rem 0;
            }
            .pq-verdict-avoid h2 {
                color: var(--pq-red);
                font-size: 2rem;
                font-weight: 900;
                margin: 0 0 0.5rem;
            }
            .pq-verdict-avoid p {
                color: var(--pq-text-muted);
                font-size: 1rem;
                margin: 0;
            }

            /* Value play cards */
            .pq-value-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1rem 1.15rem;
                margin-bottom: 0.65rem;
                transition: border-color 0.15s ease;
            }
            .pq-value-card:hover {
                border-color: var(--pq-green);
            }
            .pq-value-rank {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 28px;
                height: 28px;
                border-radius: 8px;
                background: linear-gradient(135deg, var(--pq-accent), #4f6ef7);
                color: #fff;
                font-size: 0.75rem;
                font-weight: 900;
                margin-right: 0.65rem;
                flex-shrink: 0;
            }
            .pq-value-action {
                font-size: 0.95rem;
                font-weight: 700;
                color: var(--pq-text);
                line-height: 1.4;
                margin: 0 0 0.5rem;
            }
            .pq-value-meta {
                display: flex;
                flex-wrap: wrap;
                gap: 0.5rem;
                align-items: center;
            }
            .pq-value-edge {
                display: inline-block;
                padding: 0.2rem 0.55rem;
                border-radius: 20px;
                font-size: 0.72rem;
                font-weight: 800;
                background: rgba(52,211,153,0.15);
                color: var(--pq-green);
                border: 1px solid rgba(52,211,153,0.35);
            }
            .pq-value-prob-bar {
                display: flex;
                align-items: center;
                gap: 0.35rem;
                font-size: 0.75rem;
                color: var(--pq-text-muted);
            }
            .pq-value-prob-fill {
                height: 6px;
                border-radius: 3px;
                background: var(--pq-green);
            }
            .pq-value-prob-track {
                width: 60px;
                height: 6px;
                border-radius: 3px;
                background: var(--pq-border);
                overflow: hidden;
            }

            /* Hype vs Reality bars */
            .pq-hype-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius);
                padding: 1rem;
                margin-bottom: 0.5rem;
            }
            .pq-hype-title {
                font-size: 0.88rem;
                font-weight: 700;
                color: var(--pq-text);
                margin: 0 0 0.65rem;
                line-height: 1.35;
            }
            .pq-hype-bars {
                display: grid;
                grid-template-columns: 80px 1fr 50px;
                gap: 0.35rem;
                align-items: center;
                font-size: 0.75rem;
            }
            .pq-hype-label {
                color: var(--pq-text-muted);
                font-weight: 600;
            }
            .pq-hype-bar-track {
                height: 8px;
                border-radius: 4px;
                background: var(--pq-border);
                overflow: hidden;
            }
            .pq-hype-bar-hype {
                height: 100%;
                border-radius: 4px;
                background: var(--pq-amber);
            }
            .pq-hype-bar-reality {
                height: 100%;
                border-radius: 4px;
                background: var(--pq-green);
            }
            .pq-hype-pct {
                text-align: right;
                font-weight: 700;
                font-family: var(--pq-mono);
                color: var(--pq-text);
            }
            .pq-divergence-badge {
                display: inline-block;
                padding: 0.25rem 0.65rem;
                border-radius: 20px;
                font-size: 0.72rem;
                font-weight: 800;
                margin-top: 0.5rem;
            }
            .pq-divergence-bubble {
                background: rgba(251,191,36,0.15);
                color: var(--pq-amber);
                border: 1px solid rgba(251,191,36,0.4);
            }
            .pq-divergence-value {
                background: rgba(52,211,153,0.15);
                color: var(--pq-green);
                border: 1px solid rgba(52,211,153,0.35);
            }

            /* Arb execution steps */
            .pq-exec-step {
                display: flex;
                align-items: flex-start;
                gap: 0.75rem;
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 0.85rem 1rem;
                margin-bottom: 0.45rem;
            }
            .pq-exec-num {
                width: 28px;
                height: 28px;
                border-radius: 50%;
                background: var(--pq-accent);
                color: #fff;
                font-size: 0.75rem;
                font-weight: 800;
                display: flex;
                align-items: center;
                justify-content: center;
                flex-shrink: 0;
            }
            .pq-exec-text {
                font-size: 0.88rem;
                color: var(--pq-text);
                line-height: 1.5;
            }
            .pq-exec-text strong {
                color: var(--pq-green);
            }

            /* Explore feed cards */
            .pq-explore-card {
                background: var(--pq-surface);
                border: 1px solid var(--pq-border);
                border-radius: var(--pq-radius-sm);
                padding: 0.75rem 0.9rem;
                margin-bottom: 0.35rem;
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 0.75rem;
            }
            .pq-explore-card:hover {
                border-color: var(--pq-accent);
            }
            .pq-explore-title {
                font-size: 0.85rem;
                font-weight: 600;
                color: var(--pq-text);
                line-height: 1.35;
                flex: 1;
            }
            .pq-explore-prices {
                display: flex;
                gap: 0.5rem;
                font-family: var(--pq-mono);
                font-size: 0.78rem;
                font-weight: 600;
                flex-shrink: 0;
            }
            .pq-explore-yes { color: var(--pq-green); }
            .pq-explore-no { color: var(--pq-red); }
            .pq-source-pill {
                display: inline-block;
                padding: 0.15rem 0.45rem;
                border-radius: 12px;
                font-size: 0.65rem;
                font-weight: 700;
                letter-spacing: 0.03em;
            }
            .pq-source-poly {
                background: rgba(108,140,255,0.15);
                color: var(--pq-accent);
                border: 1px solid rgba(108,140,255,0.3);
            }
            .pq-source-kalshi {
                background: rgba(251,191,36,0.12);
                color: var(--pq-amber);
                border: 1px solid rgba(251,191,36,0.3);
            }
            .pq-category-pill {
                display: inline-block;
                padding: 0.12rem 0.4rem;
                border-radius: 10px;
                font-size: 0.62rem;
                font-weight: 600;
                background: rgba(255,255,255,0.06);
                color: var(--pq-text-dim);
                border: 1px solid var(--pq-border);
            }

            /* Footer */
            .pq-footer {
                text-align: center;
                font-size: 0.72rem;
                color: var(--pq-text-dim);
                padding: 1.5rem 0 0.5rem;
                border-top: 1px solid var(--pq-border);
                margin-top: 2rem;
            }
            .pq-footer strong { color: var(--pq-text-muted); }

        </style>
        """,
        unsafe_allow_html=True,
    )
