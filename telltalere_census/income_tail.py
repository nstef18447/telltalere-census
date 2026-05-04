"""
$150k+ income tail decomposition via PUMS microdata.

The ACS bracket B25118_*_$150,000 or more is open-ended. For markets
where high-income households are a meaningful share, that single bin
hides the structure that matters for product positioning (a $150-250k
renter is in a different demand class than a $500k+ renter). PUMS
provides continuous `HINCP` per household, so we can decompose the
open-ended bracket at a higher resolution.

The module ships three primitives:

  - `decompose_high_income_tail` operates on PUMS records, filters by an
    optional tenure callable, selects records above the threshold, and
    returns weighted shares per sub-bracket.
  - `apply_tail_decomposition` rewrites a 1-D tract marginal dict so the
    top bracket entry is replaced by sub-bracket entries.
  - `apply_tail_decomposition_to_crosstab` does the same for the income
    dimension of a 2-D crosstab DataFrame, preserving each non-income
    cell's total exactly so the rake's column-sum invariant survives the
    decomposition.

Tenure filtering is the consumer's responsibility — pass a callable that
returns a boolean Series (`lambda d: d["TEN"].isin([3, 4])` for renters,
`lambda d: d["TEN"].isin([1, 2])` for owners). The package itself ships
no tenure encoding.

Sample-size flagging: `low_sample_flag` is True when the unweighted
record count above the threshold falls below `min_unweighted_n`
(default 50). Surfaced on the result dict so consumers can decide
whether to display the decomposition. Empty input (zero records pass
the filter / threshold) returns shares of all zeros and the flag set;
no NaN is propagated downstream.
"""

from __future__ import annotations

from typing import Callable, Optional, TypedDict

import pandas as pd


DEFAULT_HIGH_INCOME_THRESHOLD = 150_000.0
DEFAULT_MIN_UNWEIGHTED_N = 50


# (label, lo_inclusive, hi_exclusive). Last bracket's hi=inf is the open end.
DEFAULT_HIGH_INCOME_SUB_BRACKETS: list[tuple[str, float, float]] = [
    ("150_250k",  150_000.0,  250_000.0),
    ("250_350k",  250_000.0,  350_000.0),
    ("350_500k",  350_000.0,  500_000.0),
    ("500k_plus", 500_000.0,  float("inf")),
]


class TailDecomposition(TypedDict):
    """
    Result of decomposing a high-income tail into sub-brackets.

    Attributes:
        shares: `{sub_bracket_label: weighted_share}`. Sum equals 1.0
            (within float epsilon) when at least one record passes the
            filter + threshold; sum equals 0.0 when no records do (every
            label maps to 0.0 — no NaN propagation).
        sub_bracket_unweighted_n: per-sub-bracket unweighted record
            count. Diagnostic for sample-size questions.
        n_unweighted_above_threshold: total unweighted records above
            threshold (post-tenure-filter).
        weighted_total_above_threshold: total weighted households above
            threshold.
        low_sample_flag: True when `n_unweighted_above_threshold` is
            less than `min_unweighted_n`. Empty filtered results also
            set this.
        threshold: echoes the threshold value used.
        sub_brackets: echoes the sub_brackets list used.
    """

    shares: dict[str, float]
    sub_bracket_unweighted_n: dict[str, int]
    n_unweighted_above_threshold: int
    weighted_total_above_threshold: float
    low_sample_flag: bool
    threshold: float
    sub_brackets: list[tuple[str, float, float]]


def decompose_high_income_tail(
    pums_records: pd.DataFrame,
    tenure_filter: Optional[Callable[[pd.DataFrame], pd.Series]] = None,
    threshold: float = DEFAULT_HIGH_INCOME_THRESHOLD,
    sub_brackets: list[tuple[str, float, float]] = DEFAULT_HIGH_INCOME_SUB_BRACKETS,
    weight_col: str = "WGTP",
    income_col: str = "HINCP",
    min_unweighted_n: int = DEFAULT_MIN_UNWEIGHTED_N,
) -> TailDecomposition:
    """
    Compute the proportional split of households above a threshold across
    sub-brackets, using PUMS continuous-income data and household weights.

    Args:
        pums_records: DataFrame with `weight_col` and `income_col`
            columns plus whatever the `tenure_filter` reads. Typically
            the output of `pums_fetch.fetch_pums_data`.
        tenure_filter: callable applied to the records returning a
            boolean Series. None means "no tenure filter" (use all
            records). Use `lambda d: d["TEN"].isin([3, 4])` for renters
            or `lambda d: d["TEN"].isin([1, 2])` for owners.
        threshold: minimum income to qualify for the tail. Records with
            `HINCP >= threshold` are kept. Default 150_000.
        sub_brackets: list of `(label, lo_inclusive, hi_exclusive)`.
            Default `DEFAULT_HIGH_INCOME_SUB_BRACKETS` (150-250k /
            250-350k / 350-500k / 500k+).
        weight_col: PUMS weight column. Default "WGTP" (housing-unit
            weight); pass "PWGTP" if working with person-level data.
        income_col: PUMS income column. Default "HINCP".
        min_unweighted_n: threshold below which `low_sample_flag` is
            set. Default 50.

    Returns:
        `TailDecomposition` dict (see TypedDict above for field
        semantics). When the filter + threshold yield zero records, the
        result is fully zero-filled (`shares` map every label to 0.0,
        `sub_bracket_unweighted_n` maps every label to 0,
        `weighted_total_above_threshold` is 0.0, `low_sample_flag` is True).
    """
    df = pums_records
    if tenure_filter is not None:
        df = df[tenure_filter(df)]

    # Drop rows missing income or weight
    sub = df[[income_col, weight_col]].dropna(subset=[income_col, weight_col])
    above = sub[sub[income_col] >= threshold]

    n_unweighted = int(len(above))
    weighted_total = float(above[weight_col].sum()) if n_unweighted > 0 else 0.0

    # Empty case — zero-fill, flag low sample, return without NaN
    if n_unweighted == 0 or weighted_total <= 0.0:
        return {
            "shares": {label: 0.0 for label, _, _ in sub_brackets},
            "sub_bracket_unweighted_n": {label: 0 for label, _, _ in sub_brackets},
            "n_unweighted_above_threshold": n_unweighted,
            "weighted_total_above_threshold": weighted_total,
            "low_sample_flag": True,
            "threshold": float(threshold),
            "sub_brackets": list(sub_brackets),
        }

    shares: dict[str, float] = {}
    counts: dict[str, int] = {}
    for label, lo, hi in sub_brackets:
        in_bracket = above[(above[income_col] >= lo) & (above[income_col] < hi)]
        counts[label] = int(len(in_bracket))
        shares[label] = float(in_bracket[weight_col].sum()) / weighted_total

    return {
        "shares": shares,
        "sub_bracket_unweighted_n": counts,
        "n_unweighted_above_threshold": n_unweighted,
        "weighted_total_above_threshold": weighted_total,
        "low_sample_flag": n_unweighted < min_unweighted_n,
        "threshold": float(threshold),
        "sub_brackets": list(sub_brackets),
    }


def apply_tail_decomposition(
    tract_marginal: dict[str, int],
    tail_shares: dict[str, float],
    top_bracket_label: str = "150k_plus",
) -> dict[str, int]:
    """
    Replace a tract marginal's `top_bracket_label` count with sub-bracket
    counts allocated by `tail_shares`.

    Args:
        tract_marginal: `{bin_id: int_count}`, typically the output of
            `tract_marginals.compute_tract_marginal`. Must contain
            `top_bracket_label`.
        tail_shares: `{sub_bracket_label: share}`, typically
            `TailDecomposition["shares"]`. Shares should sum to 1.0; the
            function does not normalize.
        top_bracket_label: which key in `tract_marginal` to replace.
            Default "150k_plus".

    Returns:
        A new dict with `top_bracket_label` removed and one entry per
        `tail_shares` key added. The total integer sum is preserved
        exactly: rounding drift is allocated to the largest sub-bracket
        by share so `sum(result.values()) == sum(tract_marginal.values())`.

    Raises:
        KeyError: if `top_bracket_label` is not in `tract_marginal`.
        ValueError: if any sub-bracket label collides with an existing
            key in `tract_marginal` other than `top_bracket_label`.
        ValueError: if `tail_shares` is empty.
    """
    if top_bracket_label not in tract_marginal:
        raise KeyError(
            f"top_bracket_label {top_bracket_label!r} not present in marginal "
            f"(keys: {list(tract_marginal.keys())})"
        )
    if not tail_shares:
        raise ValueError("tail_shares is empty")

    other_keys = set(tract_marginal) - {top_bracket_label}
    collisions = other_keys & set(tail_shares)
    if collisions:
        raise ValueError(
            f"sub-bracket labels collide with existing marginal keys: "
            f"{sorted(collisions)}"
        )

    top_count = int(tract_marginal[top_bracket_label])
    sub_counts = _allocate_with_drift(top_count, tail_shares)

    out: dict[str, int] = {k: int(v) for k, v in tract_marginal.items() if k != top_bracket_label}
    out.update(sub_counts)
    return out


def apply_tail_decomposition_to_crosstab(
    crosstab: pd.DataFrame,
    tail_shares: dict[str, float],
    top_bracket_label: str = "150k_plus",
    income_dim: str = "income_bin",
    weight_col: str = "weight",
) -> pd.DataFrame:
    """
    Apply tail decomposition to the top-income row of an income x other
    crosstab, preserving each non-income cell's total exactly.

    The crosstab is expected in long form — a MultiIndex DataFrame with
    one of the levels named `income_dim` and a single value column
    (`weight` by default). This is the shape produced by
    `puma_crosstab.compute_puma_joint` and `column_major_to_joint`.

    For each unique value of the other dimension, the cell at
    `(income == top_bracket_label, other == val)` is replaced with one
    cell per `tail_shares` key, allocated by share with rounding drift
    going to the largest sub-bracket *within that other-dim group*.

    The per-other-dim allocation policy (rather than computing global
    shares once and scattering) preserves the rake's column-sum
    invariant: each other-dim total still equals the input crosstab's
    column sum at that value. The trade-off is that `cell == round(
    global_share * top_in_that_column)` may differ by ±1 from `round(
    global_share) * top_in_that_column` due to per-group drift; the
    column-sum invariant is the consumer-facing contract.

    Args:
        crosstab: long-format crosstab DataFrame with a 2-level
            MultiIndex (one level named `income_dim`) and a single
            `weight_col` column.
        tail_shares: `{sub_bracket_label: share}`, sums should equal
            1.0; not normalized by the function.
        top_bracket_label: which value in `income_dim` to replace.
            Default "150k_plus".
        income_dim: name of the income index level. Default "income_bin".
        weight_col: name of the value column. Default "weight".

    Returns:
        New DataFrame with the same MultiIndex level names; rows for
        `(top_bracket_label, *)` are dropped and replaced by rows
        for each `(sub_label, *)`. Other rows pass through unchanged.

    Raises:
        ValueError: if `crosstab` does not have exactly 2 index levels,
            or if `income_dim` is not one of those levels.
        ValueError: if any sub-bracket label collides with an existing
            value in the income dimension other than `top_bracket_label`.
    """
    if crosstab.index.nlevels != 2:
        raise ValueError(
            f"crosstab must have 2 index levels; got {crosstab.index.nlevels}"
        )
    if income_dim not in crosstab.index.names:
        raise ValueError(
            f"income_dim {income_dim!r} is not in the crosstab index names "
            f"({list(crosstab.index.names)})"
        )

    income_values = set(crosstab.index.get_level_values(income_dim))
    collisions = (income_values - {top_bracket_label}) & set(tail_shares)
    if collisions:
        raise ValueError(
            f"sub-bracket labels collide with existing income values: "
            f"{sorted(collisions)}"
        )

    other_dim = next(n for n in crosstab.index.names if n != income_dim)
    keep_mask = crosstab.index.get_level_values(income_dim) != top_bracket_label
    survivors = crosstab[keep_mask].copy()

    top_rows = crosstab[~keep_mask]
    new_rows: list[tuple[str, str, int]] = []  # (income_val, other_val, weight)
    for idx, row in top_rows.iterrows():
        # idx is a tuple — find the other-dim value by index level
        as_named = dict(zip(crosstab.index.names, idx))
        other_val = as_named[other_dim]
        top_count = int(row[weight_col])
        sub_counts = _allocate_with_drift(top_count, tail_shares)
        for label, count in sub_counts.items():
            if income_dim == crosstab.index.names[0]:
                new_rows.append((label, str(other_val), int(count)))
            else:
                new_rows.append((str(other_val), label, int(count)))

    if not new_rows:
        return survivors
    new_df = pd.DataFrame(new_rows, columns=[*crosstab.index.names, weight_col])
    new_df = new_df.set_index(list(crosstab.index.names))
    new_df[weight_col] = new_df[weight_col].astype("int64")
    out = pd.concat([survivors, new_df])
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _allocate_with_drift(
    total: int, shares: dict[str, float],
) -> dict[str, int]:
    """
    Round-allocate `total` across `shares` preserving the integer sum.

    Each label gets `round(total * share)`; the rounding drift (signed
    difference between `total` and `sum(rounded)`) is added to (or
    subtracted from) the label with the largest share. Cells stay >= 0.

    Empty `shares` raises ValueError. `total <= 0` returns all-zero.
    """
    if not shares:
        raise ValueError("shares is empty")
    if total <= 0:
        return {label: 0 for label in shares}

    rounded = {label: int(round(total * float(share))) for label, share in shares.items()}
    drift = total - sum(rounded.values())
    if drift != 0:
        # Adjust the largest-share label. Stable on ties via dict insertion order.
        biggest = max(shares.items(), key=lambda kv: kv[1])[0]
        rounded[biggest] = max(0, rounded[biggest] + drift)
        # If clamping at zero pushed the actual sum away from `total`, reflow
        # any remaining drift to subsequent labels in share-descending order.
        actual = sum(rounded.values())
        residual = total - actual
        if residual != 0:
            order = sorted(shares.items(), key=lambda kv: -kv[1])
            for label, _share in order:
                if residual == 0:
                    break
                if residual > 0:
                    rounded[label] += residual
                    residual = 0
                else:
                    take = min(rounded[label], -residual)
                    rounded[label] -= take
                    residual += take
    return rounded
