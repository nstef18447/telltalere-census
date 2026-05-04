"""
Configurable (age_bin, income_bin) -> lifestage rollup.

A `LifestageGrid` is an explicit mapping from cell (age_bin, income_bin)
pairs to a lifestage label. The package ships one default grid
(`RCLCO_DEFAULT_LIFESTAGE_GRID`, age-only — every income bin in a given
age row maps to the same lifestage) but consumers can pass any grid
that fits their analysis. The mechanism is generic; the grid shape is
not enforced beyond "every cell in the input crosstab must be mapped".

Safety property: `aggregate_to_lifestages` raises `KeyError` (rather
than silently dropping) on cells the grid doesn't map. This catches
bin-scheme drift early — if a consumer adds a new income bin to their
ACS rollup but forgets to extend the lifestage grid, the test fails
loudly instead of silently zeroing data.

Future config-loader note: `LifestageGrid` uses tuple keys
(`{(age_bin, income_bin): label}`), which don't round-trip through
JSON / YAML. When a consumer needs to load grids from a config file,
add a `LifestageGrid.from_records([{age, income, label}, ...])`
classmethod. Out of scope for v1; module-level Python defaults are
sufficient.
"""

from __future__ import annotations

from typing import Literal, TypedDict

import pandas as pd


class LifestageGrid(TypedDict):
    """A configurable mapping from (age_bin, income_bin) cells to lifestage labels.

    Attributes:
        cells: `{(age_bin, income_bin): label}`. Every cell that may
            appear in an input crosstab must be present, or
            `aggregate_to_lifestages` will raise.
        label_order: canonical display order of the labels appearing
            as values in `cells`. The output dict iterates in this
            order. Labels in `cells` not in `label_order` cause an
            error at output time (defensive, surfaces drift early).
    """

    cells: dict[tuple[str, str], str]
    label_order: list[str]


# Standard ACS bin labels — match what Core_BTR's
# config/demographic_bins.py exports as INCOME_BIN_IDS / AGE_BIN_IDS.
_DEFAULT_AGE_BINS = ["under_25", "25_34", "35_44", "45_54", "55_64", "65_plus"]
_DEFAULT_INCOME_BINS = ["under_35k", "35_50k", "50_75k", "75_100k", "100_150k", "150k_plus"]

# v1 age-only RCLCO scheme: every income bin in a given age row maps to
# the same label. Income segmentation (Affordable / Workforce / Market /
# Luxury overlays) is a future-session concern; the cell-grid shape
# leaves room for it.
RCLCO_DEFAULT_LIFESTAGE_GRID: LifestageGrid = {
    "cells": {
        **{("under_25", inc): "Post-Grad"           for inc in _DEFAULT_INCOME_BINS},
        **{("25_34",    inc): "Young Professional"  for inc in _DEFAULT_INCOME_BINS},
        **{("35_44",    inc): "Family"              for inc in _DEFAULT_INCOME_BINS},
        **{("45_54",    inc): "Family"              for inc in _DEFAULT_INCOME_BINS},
        **{("55_64",    inc): "Mature Professional" for inc in _DEFAULT_INCOME_BINS},
        **{("65_plus",  inc): "Empty Nester"        for inc in _DEFAULT_INCOME_BINS},
    },
    "label_order": [
        "Post-Grad",
        "Young Professional",
        "Family",
        "Mature Professional",
        "Empty Nester",
    ],
}


def aggregate_to_lifestages(
    income_age_crosstab: pd.DataFrame,
    grid: LifestageGrid,
    age_dim: str = "age_bin",
    income_dim: str = "income_bin",
    weight_col: str = "weight",
) -> dict[str, int]:
    """
    Aggregate an income x age crosstab to lifestage labels.

    Args:
        income_age_crosstab: long-form crosstab with a 2-level
            MultiIndex containing both `age_dim` and `income_dim`,
            and a single `weight_col` column. Output of
            `puma_crosstab.compute_puma_joint` or
            `puma_crosstab.column_major_to_joint`.
        grid: `LifestageGrid` describing how each (age, income) cell
            rolls up. Every cell in the crosstab must be present in
            `grid["cells"]`.
        age_dim, income_dim: index level names. Defaults `"age_bin"`,
            `"income_bin"`.
        weight_col: value column. Default `"weight"`.

    Returns:
        `{lifestage_label: int_count}`, ordered per `grid["label_order"]`.
        Sum equals the input crosstab's `weight_col` total exactly
        (after rounding); rounding drift goes to the largest-receiving
        label.

    Raises:
        ValueError: if the crosstab does not have exactly 2 index
            levels, or if `age_dim` / `income_dim` are not present.
        KeyError: if the crosstab contains a (age, income) cell that
            is not mapped in `grid["cells"]`. This is a deliberate
            safety property — silent drops would mask bin-scheme drift.
        ValueError: if `grid["cells"]` references a label not in
            `grid["label_order"]`.
    """
    if income_age_crosstab.index.nlevels != 2:
        raise ValueError(
            f"crosstab must have 2 index levels; got {income_age_crosstab.index.nlevels}"
        )
    level_names = list(income_age_crosstab.index.names)
    if age_dim not in level_names or income_dim not in level_names:
        raise ValueError(
            f"crosstab is missing one of age_dim={age_dim!r} or "
            f"income_dim={income_dim!r}; index has {level_names}"
        )

    # Validate label_order covers every label referenced by cells.
    label_set = set(grid["label_order"])
    cell_labels = set(grid["cells"].values())
    extra = cell_labels - label_set
    if extra:
        raise ValueError(
            f"grid['cells'] references labels not in grid['label_order']: "
            f"{sorted(extra)}"
        )

    # Build float-accumulator per label, then round + drift-allocate.
    accum = {label: 0.0 for label in grid["label_order"]}
    for idx, row in income_age_crosstab.iterrows():
        as_named = dict(zip(level_names, idx))
        cell = (str(as_named[age_dim]), str(as_named[income_dim]))
        if cell not in grid["cells"]:
            raise KeyError(
                f"crosstab cell {cell} is not mapped in grid['cells']. "
                "Add it to the grid (or remove the row from the input) — "
                "silent drops are not allowed."
            )
        accum[grid["cells"][cell]] += float(row[weight_col])

    return _allocate_rounded(accum, grid["label_order"])


def aggregate_to_lifestages_with_tail(
    crosstab_with_tail: pd.DataFrame,
    grid: LifestageGrid,
    tail_treatment: Literal["roll_up", "split"] = "roll_up",
    tail_sub_brackets: tuple[str, ...] = ("150_250k", "250_350k", "350_500k", "500k_plus"),
    tail_rolled_label: str = "150k_plus",
    age_dim: str = "age_bin",
    income_dim: str = "income_bin",
    weight_col: str = "weight",
) -> dict[str, int]:
    """
    Apply lifestage rollup to a crosstab whose income dimension has been
    decomposed into $150k+ sub-brackets via `apply_tail_decomposition_to_crosstab`.

    `tail_treatment="roll_up"` (default): collapse `tail_sub_brackets`
    back into a single `tail_rolled_label` bin before aggregating. Use
    when the grid is defined for the standard 6-bin income scheme (the
    case for `RCLCO_DEFAULT_LIFESTAGE_GRID`).

    `tail_treatment="split"`: requires the grid to define cells for
    every sub-bracket explicitly. Raises `KeyError` (via
    `aggregate_to_lifestages`) if any sub-bracket cell is unmapped.

    Args:
        crosstab_with_tail: long-form crosstab where the income
            dimension may contain either the rolled label
            (`"150k_plus"`) or the sub-bracket labels (`"150_250k"`
            etc.). Mixed presence is allowed in the roll_up path.
        grid: lifestage grid.
        tail_treatment: see above.
        tail_sub_brackets: sub-bracket income-bin labels to fold (in
            roll_up mode) or pass through (in split mode). Default
            matches `income_tail.DEFAULT_HIGH_INCOME_SUB_BRACKETS`.
        tail_rolled_label: target label in roll_up mode. Default
            `"150k_plus"`. Must be a key in the grid for the relevant
            age rows.
        age_dim, income_dim, weight_col: as in
            `aggregate_to_lifestages`.

    Returns:
        Same shape as `aggregate_to_lifestages`.
    """
    if tail_treatment == "split":
        return aggregate_to_lifestages(
            crosstab_with_tail, grid,
            age_dim=age_dim, income_dim=income_dim, weight_col=weight_col,
        )

    if tail_treatment != "roll_up":
        raise ValueError(
            f"tail_treatment must be 'roll_up' or 'split'; got {tail_treatment!r}"
        )

    # Roll the sub-brackets back into the standard $150k+ bin.
    df = crosstab_with_tail.reset_index()
    sub_set = set(tail_sub_brackets)
    df.loc[df[income_dim].isin(sub_set), income_dim] = tail_rolled_label
    rolled = (
        df.groupby([income_dim, age_dim] if income_dim == crosstab_with_tail.index.names[0]
                   else [age_dim, income_dim], observed=True)[weight_col]
        .sum().to_frame(name=weight_col)
    )
    return aggregate_to_lifestages(
        rolled, grid, age_dim=age_dim, income_dim=income_dim, weight_col=weight_col,
    )


# ---------------------------------------------------------------------------
# Internal: rounded allocation
# ---------------------------------------------------------------------------

def _allocate_rounded(
    accum: dict[str, float],
    label_order: list[str],
) -> dict[str, int]:
    """
    Round each accumulator and reconcile total via largest-receiver drift.

    Used internally by `aggregate_to_lifestages` so the lifestage rollup's
    integer total exactly matches the input crosstab's total (which the
    consumer typically asserts equals the tract's authoritative tenure
    subtotal post-rake).
    """
    rounded = {label: int(round(accum.get(label, 0.0))) for label in label_order}
    target = int(round(sum(accum.get(label, 0.0) for label in label_order)))
    drift = target - sum(rounded.values())
    if drift != 0 and rounded:
        # Push drift to the largest accumulator (not the largest rounded
        # value, to be deterministic on ties when multiple labels rounded
        # to the same int).
        biggest = max(label_order, key=lambda label: accum.get(label, 0.0))
        rounded[biggest] = max(0, rounded[biggest] + drift)
        # If clamping at zero introduced residual drift, reflow.
        actual = sum(rounded.values())
        residual = target - actual
        if residual != 0:
            order = sorted(label_order, key=lambda label: -accum.get(label, 0.0))
            for label in order:
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
