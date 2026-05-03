"""
Proportional reallocation of ACS bracketed counts onto a target interval.

`proportional_from_brackets` answers: given a row with ACS bracket counts
(e.g. household income brackets B25118_015E..025E) and a target interval
[target_low, target_high), what fraction of the denominator falls in
that interval?

The function assumes uniform distribution within each closed bracket and
treats the open-ended top bracket as fully included whenever the target
interval reaches its floor. Suppressed bracket cells (NaN / None) are
treated as 0 — they do not poison the denominator. A computed share above
1.0 + 1e-9 raises (defensive: indicates a bracket-enumeration bug in the
caller's configuration).

This module is a verbatim behavioral port of Core_BTR's bg_demand
`_proportional_from_brackets` helper, exposed under a public name and
parameter naming that does not bake in renter / income-specific concepts.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd


def proportional_from_brackets(
    row: pd.Series,
    denom_col: str,
    closed_brackets: list[tuple[str, float, float]],
    top_code: str,
    top_floor: float,
    target_low: float,
    target_high: float,
) -> Optional[float]:
    """
    Compute the share of the denominator falling within [target_low, target_high)
    using proportional allocation across published brackets.

    Args:
        row: ACS DataFrame row containing the denominator and bracket cells.
        denom_col: column name of the row's denominator (e.g. the renter-
            occupied subtotal "B25118_014E"). Returns None if the cell is
            NaN, None, or 0.
        closed_brackets: list of (variable_code, lo_inclusive, hi_exclusive)
            tuples for each closed-interval bracket. Within each bracket
            the count is assumed uniformly distributed across [lo, hi).
        top_code: variable code for the open-ended top bracket
            (e.g. "B25118_025E" for "$150,000 or more").
        top_floor: lower bound of the open-ended top bracket
            (e.g. 150000.0). The top bracket contributes its full count
            iff `target_high >= top_floor`.
        target_low, target_high: half-open target interval [target_low, target_high).
            For closed brackets the contribution is the count times the
            length of overlap divided by the bracket width.

    Returns:
        Share in [0, 1], or None if the denominator is missing / zero.

    Raises:
        RuntimeError: if the computed share exceeds 1.0 + 1e-9. This
            indicates the caller's bracket configuration over-counts the
            denominator — a configuration bug, not a data condition.
    """
    denom = row.get(denom_col)
    if pd.isna(denom) or denom is None or denom == 0:
        return None

    total_included = 0.0

    for code, lo, hi in closed_brackets:
        cnt = row.get(code)
        if pd.isna(cnt) or cnt is None:
            continue
        overlap_lo = max(lo, target_low)
        overlap_hi = min(hi, target_high)
        if overlap_hi <= overlap_lo or hi <= lo:
            continue
        fraction = (overlap_hi - overlap_lo) / (hi - lo)
        fraction = max(0.0, min(1.0, fraction))
        total_included += fraction * float(cnt)

    if target_high >= top_floor:
        top_cnt = row.get(top_code)
        if not (pd.isna(top_cnt) or top_cnt is None):
            total_included += float(top_cnt)

    share = total_included / float(denom)
    if share > 1.0 + 1e-9:
        raise RuntimeError(
            f"proportional allocation produced share={share:.6f} > 1.0 against "
            f"denominator column {denom_col}. Bracket-enumeration bug."
        )
    return min(share, 1.0)
