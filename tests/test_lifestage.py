"""
Tests for lifestage rollup.

Coverage:
  - aggregate_to_lifestages: basic 2-D rollup, total preservation,
    output ordering follows label_order, KeyError on unmapped cell,
    ValueError on misshapen index, ValueError on label not in label_order.
  - aggregate_to_lifestages_with_tail "roll_up": result equals applying
    rollup to the un-decomposed crosstab (same totals before vs after
    decompose-then-roll-up).
  - aggregate_to_lifestages_with_tail "split": passes through to
    aggregate_to_lifestages — KeyError when grid is missing a
    sub-bracket cell.
  - Default grid: RCLCO label set + age coverage.
"""

from __future__ import annotations

import pandas as pd
import pytest

from telltalere_census.income_tail import (
    DEFAULT_HIGH_INCOME_SUB_BRACKETS,
    apply_tail_decomposition_to_crosstab,
)
from telltalere_census.lifestage import (
    LifestageGrid,
    RCLCO_DEFAULT_LIFESTAGE_GRID,
    aggregate_to_lifestages,
    aggregate_to_lifestages_with_tail,
)


def _make_crosstab(rows, row_dim="income_bin", col_dim="age_bin"):
    df = pd.DataFrame(rows, columns=[row_dim, col_dim, "weight"])
    return df.set_index([row_dim, col_dim])


# ---------------------------------------------------------------------------
# RCLCO default grid sanity
# ---------------------------------------------------------------------------

def test_rclco_default_grid_has_5_labels():
    assert RCLCO_DEFAULT_LIFESTAGE_GRID["label_order"] == [
        "Post-Grad", "Young Professional", "Family",
        "Mature Professional", "Empty Nester",
    ]


def test_rclco_default_grid_covers_36_cells():
    assert len(RCLCO_DEFAULT_LIFESTAGE_GRID["cells"]) == 36  # 6 ages × 6 incomes


def test_rclco_default_age_only_collapses_income():
    """For any fixed age, every income bin maps to the same label."""
    grid = RCLCO_DEFAULT_LIFESTAGE_GRID["cells"]
    for age in ["under_25", "25_34", "35_44", "45_54", "55_64", "65_plus"]:
        labels_in_age = {grid[(age, inc)] for inc in
                         ["under_35k", "35_50k", "50_75k", "75_100k", "100_150k", "150k_plus"]}
        assert len(labels_in_age) == 1, f"age {age} maps to multiple labels: {labels_in_age}"


# ---------------------------------------------------------------------------
# aggregate_to_lifestages
# ---------------------------------------------------------------------------

def test_aggregate_basic_with_rclco_grid():
    crosstab = _make_crosstab([
        ("under_35k", "under_25", 100),
        ("under_35k", "25_34",    50),
        ("50_75k",    "35_44",    80),
        ("100_150k",  "65_plus",  30),
    ])
    out = aggregate_to_lifestages(crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID)
    assert out["Post-Grad"] == 100
    assert out["Young Professional"] == 50
    assert out["Family"] == 80
    assert out["Empty Nester"] == 30
    assert out["Mature Professional"] == 0
    # Sum preserved exactly
    assert sum(out.values()) == 260


def test_aggregate_output_follows_label_order():
    crosstab = _make_crosstab([("under_35k", "65_plus", 50)])
    out = aggregate_to_lifestages(crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID)
    # Insertion order is the label_order
    assert list(out.keys()) == RCLCO_DEFAULT_LIFESTAGE_GRID["label_order"]


def test_aggregate_handles_swapped_index_order():
    # Index level order is (age_bin, income_bin) instead of the usual
    # (income_bin, age_bin) — the function must look up by name.
    crosstab = _make_crosstab(
        [("25_34", "under_35k", 70), ("65_plus", "150k_plus", 20)],
        row_dim="age_bin", col_dim="income_bin",
    )
    out = aggregate_to_lifestages(crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID)
    assert out["Young Professional"] == 70
    assert out["Empty Nester"] == 20


def test_aggregate_unmapped_cell_raises_keyerror():
    crosstab = _make_crosstab([("rare_bin", "65_plus", 5)])
    with pytest.raises(KeyError, match="not mapped in grid"):
        aggregate_to_lifestages(crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID)


def test_aggregate_grid_label_not_in_order_raises():
    bad_grid: LifestageGrid = {
        "cells": {("under_25", "under_35k"): "FloatingLabel"},
        "label_order": ["A", "B"],  # missing FloatingLabel
    }
    crosstab = _make_crosstab([("under_35k", "under_25", 5)])
    with pytest.raises(ValueError, match="not in grid\\['label_order'\\]"):
        aggregate_to_lifestages(crosstab, bad_grid)


def test_aggregate_invalid_index_levels_raises():
    df = pd.DataFrame({"weight": [1]}, index=pd.Index(["only"], name="x"))
    with pytest.raises(ValueError, match="2 index levels"):
        aggregate_to_lifestages(df, RCLCO_DEFAULT_LIFESTAGE_GRID)


def test_aggregate_missing_dim_raises():
    crosstab = _make_crosstab([("under_35k", "25_34", 1)])
    with pytest.raises(ValueError, match="missing"):
        aggregate_to_lifestages(crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID,
                                age_dim="not_there")


def test_aggregate_rounding_preserves_total():
    """A cell with fractional weights (post-IPF imputation) still rolls up
    cleanly — drift goes to the largest receiver."""
    # Use float weights to force rounding
    df = pd.DataFrame([
        ("under_35k", "under_25", 33.4),
        ("under_35k", "25_34",    33.3),
        ("50_75k",    "65_plus",  33.3),
    ], columns=["income_bin", "age_bin", "weight"]).set_index(["income_bin", "age_bin"])
    out = aggregate_to_lifestages(df, RCLCO_DEFAULT_LIFESTAGE_GRID)
    # Input float total = 100.0; rounded total = 100
    assert sum(out.values()) == 100


# ---------------------------------------------------------------------------
# aggregate_to_lifestages_with_tail (roll_up)
# ---------------------------------------------------------------------------

def test_with_tail_roll_up_equals_undecomposed():
    """Decompose a $150k+ bin then roll it back up. The lifestage totals
    must equal applying the grid directly to the undecomposed crosstab."""
    base = _make_crosstab([
        ("under_35k", "under_25", 50),
        ("under_35k", "25_34",    40),
        ("150k_plus", "35_44",    100),  # the bin we'll decompose
        ("150k_plus", "65_plus",  60),
    ])
    direct = aggregate_to_lifestages(base, RCLCO_DEFAULT_LIFESTAGE_GRID)

    decomposed = apply_tail_decomposition_to_crosstab(
        base,
        tail_shares={"150_250k": 0.4, "250_350k": 0.3, "350_500k": 0.2, "500k_plus": 0.1},
    )
    via_tail = aggregate_to_lifestages_with_tail(
        decomposed, RCLCO_DEFAULT_LIFESTAGE_GRID, tail_treatment="roll_up",
    )
    assert via_tail == direct


def test_with_tail_roll_up_handles_mixed_presence():
    """A crosstab where only some sub-bracket cells exist (others were
    zero so weren't emitted): roll_up should still tally correctly."""
    crosstab = _make_crosstab([
        ("under_35k", "25_34", 10),
        ("150_250k",  "35_44", 60),
        ("500k_plus", "35_44", 40),
    ])
    out = aggregate_to_lifestages_with_tail(
        crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID, tail_treatment="roll_up",
    )
    # Family receives 60+40 = 100 (both sub-brackets roll back to 150k_plus)
    assert out["Young Professional"] == 10
    assert out["Family"] == 100


def test_with_tail_split_passes_through_when_grid_is_complete():
    # Build a grid that explicitly maps each sub-bracket cell.
    sub_labels = [b[0] for b in DEFAULT_HIGH_INCOME_SUB_BRACKETS]
    cells: dict[tuple[str, str], str] = dict(RCLCO_DEFAULT_LIFESTAGE_GRID["cells"])
    for age in ["under_25", "25_34", "35_44", "45_54", "55_64", "65_plus"]:
        for sub in sub_labels:
            # Same label as the existing 150k_plus mapping for that age
            cells[(age, sub)] = cells[(age, "150k_plus")]
    extended_grid: LifestageGrid = {
        "cells": cells,
        "label_order": list(RCLCO_DEFAULT_LIFESTAGE_GRID["label_order"]),
    }

    crosstab = _make_crosstab([
        ("150_250k", "35_44", 30),
        ("500k_plus", "65_plus", 20),
    ])
    out = aggregate_to_lifestages_with_tail(
        crosstab, extended_grid, tail_treatment="split",
    )
    assert out["Family"] == 30
    assert out["Empty Nester"] == 20


def test_with_tail_split_fails_when_grid_incomplete():
    crosstab = _make_crosstab([("150_250k", "35_44", 30)])
    # Default grid has no entry for ("35_44", "150_250k") — it expects
    # the rolled "150k_plus" income bin.
    with pytest.raises(KeyError, match="not mapped"):
        aggregate_to_lifestages_with_tail(
            crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID, tail_treatment="split",
        )


def test_with_tail_invalid_treatment_raises():
    crosstab = _make_crosstab([("under_35k", "25_34", 10)])
    with pytest.raises(ValueError, match="must be 'roll_up' or 'split'"):
        aggregate_to_lifestages_with_tail(
            crosstab, RCLCO_DEFAULT_LIFESTAGE_GRID, tail_treatment="bogus",  # type: ignore[arg-type]
        )
