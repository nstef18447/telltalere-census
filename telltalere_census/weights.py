"""
Weight-aware aggregation helpers for PUMS microdata.

PUMS rows carry a `WGTP` (housing weight) column. Naive counts are wrong —
analysis always needs weighted sums or weighted averages. These helpers
centralize the patterns so callers don't re-inline `sum(WGTP)` /
`np.average(..., weights=WGTP)` everywhere.

All helpers assume the DataFrame contains a numeric `WGTP` column. They do
not mutate the input.
"""

from __future__ import annotations

from typing import Optional, Union

import numpy as np
import pandas as pd

WEIGHT_COL = "WGTP"


def weighted_sum(df: pd.DataFrame, mask: Optional[pd.Series] = None) -> float:
    """
    Sum of WGTP over rows matching `mask` (or the whole frame if None).

    Returns a float. Empty inputs return 0.0.
    """
    if mask is None:
        return float(df[WEIGHT_COL].sum())
    return float(df.loc[mask, WEIGHT_COL].sum())


def weighted_count_by(
    df: pd.DataFrame,
    by: Union[str, list[str]],
    na_label: str = "__na__",
) -> pd.Series:
    """
    Weighted row count grouped by the given column(s). NaN values in the
    grouper are preserved under `na_label`.

    Returns a Series indexed by the group key(s), sorted by index.
    """
    if isinstance(by, str):
        key = df[by].fillna(na_label)
    else:
        key = [df[c].fillna(na_label) for c in by]
    return df.groupby(key)[WEIGHT_COL].sum().sort_index()


def weighted_mean(
    df: pd.DataFrame,
    value_col: str,
    mask: Optional[pd.Series] = None,
) -> Optional[float]:
    """
    Weighted mean of `value_col` using WGTP as weights.

    Returns None if the effective weight is zero or no rows match. NaN values
    in `value_col` are excluded from the calculation.
    """
    if mask is not None:
        sub = df.loc[mask]
    else:
        sub = df
    sub = sub[sub[value_col].notna()]
    if len(sub) == 0:
        return None
    total_w = float(sub[WEIGHT_COL].sum())
    if total_w <= 0:
        return None
    return float(np.average(sub[value_col], weights=sub[WEIGHT_COL]))


def weighted_share(
    df: pd.DataFrame,
    numerator_mask: pd.Series,
    denominator_mask: Optional[pd.Series] = None,
) -> Optional[float]:
    """
    Share of weighted rows where `numerator_mask` is True, out of rows where
    `denominator_mask` is True (or all rows if denominator is None).

    Returns None if the denominator sum is zero.
    """
    if denominator_mask is None:
        denom = float(df[WEIGHT_COL].sum())
    else:
        denom = float(df.loc[denominator_mask, WEIGHT_COL].sum())
    if denom == 0:
        return None
    numer = float(df.loc[numerator_mask, WEIGHT_COL].sum())
    return numer / denom
