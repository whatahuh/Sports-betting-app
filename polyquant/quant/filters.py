"""Dual Mandate value play filter."""
from __future__ import annotations

import pandas as pd

from polyquant.config import MIN_VOLUME, VALUE_PLAYS_EV_EDGE_MIN, VALUE_PLAYS_MAX, VALUE_PLAYS_WIN_MIN
from polyquant.quant.ev import enrich_value_plays


def filter_value_plays(raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Dual Mandate: model_prob > 75% AND fee-adjusted EV edge >= 4.5%.
    Sort by edge descending, cap at max_plays.
    """
    out = enrich_value_plays(raw_df)
    if out.empty:
        return out
    out = out[out["Volume"] >= MIN_VOLUME]
    out = out[out["Model Win %"] > VALUE_PLAYS_WIN_MIN]
    out = out[out["Net EV Edge %"] >= VALUE_PLAYS_EV_EDGE_MIN]
    return (
        out.sort_values("Net EV Edge %", ascending=False)
        .reset_index(drop=True)
        .head(VALUE_PLAYS_MAX)
    )
