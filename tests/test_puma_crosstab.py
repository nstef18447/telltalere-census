"""
Synthetic / hand-computable tests for puma_crosstab.

Live PUMA-level parity against Core_BTR's compute_puma_crosstabs lives
in test_core_btr_parity.py.

Coverage:
  - 1-D and 2-D joints from a contrived PUMS-shaped DataFrame
  - Weight column override (PWGTP vs WGTP)
  - filter_func excludes non-matching rows
  - NaN bin labels and NaN weights are dropped (matches Core_BTR convention)
  - Empty input / all-filtered-out returns an empty result with the
    correct index/column shape
  - Adapters round-trip: joint -> column_major -> joint preserves data
  - joint_to_column_major zero-fills missing combinations using the
    declared row_order / col_order
"""

from __future__ import annotations

import pandas as pd
import pytest

from telltalere_census.puma_crosstab import (
    column_major_to_joint,
    compute_puma_joint,
    joint_to_column_major,
)


def make_pums_df() -> pd.DataFrame:
    # 6 records: 3 renters (TEN in {3,4}), 3 owners (TEN in {1,2}).
    # Two income bins, two age bins.
    return pd.DataFrame([
        {"TEN": 3, "income_bin": "low",  "age_bin": "young", "WGTP": 10, "PWGTP": 5},
        {"TEN": 3, "income_bin": "low",  "age_bin": "young", "WGTP": 20, "PWGTP": 7},
        {"TEN": 4, "income_bin": "high", "age_bin": "old",   "WGTP": 30, "PWGTP": 9},
        {"TEN": 1, "income_bin": "low",  "age_bin": "young", "WGTP": 40, "PWGTP": 12},
        {"TEN": 1, "income_bin": "high", "age_bin": "old",   "WGTP": 50, "PWGTP": 15},
        {"TEN": 2, "income_bin": "high", "age_bin": "young", "WGTP": 60, "PWGTP": 18},
    ])


def test_1d_marginal_renter_filter():
    df = make_pums_df()
    out = compute_puma_joint(
        df, dimensions=["income_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    # Renters: low = 10+20 = 30; high = 30
    assert out.loc["low", "weight"] == 30
    assert out.loc["high", "weight"] == 30
    assert out["weight"].sum() == 60


def test_2d_joint_renter_filter():
    df = make_pums_df()
    out = compute_puma_joint(
        df, dimensions=["income_bin", "age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    # Renters only: (low, young) → 30; (high, old) → 30
    assert out.loc[("low", "young"), "weight"] == 30
    assert out.loc[("high", "old"), "weight"] == 30
    assert len(out) == 2
    assert list(out.index.names) == ["income_bin", "age_bin"]


def test_owner_filter_via_filter_func():
    df = make_pums_df()
    out = compute_puma_joint(
        df, dimensions=["income_bin"],
        filter_func=lambda d: d["TEN"].isin([1, 2]),
    )
    # Owners: low=40; high=50+60=110
    assert out.loc["low", "weight"] == 40
    assert out.loc["high", "weight"] == 110


def test_no_filter_uses_all_rows():
    df = make_pums_df()
    out = compute_puma_joint(df, dimensions=["income_bin"])
    # All records: low = 10+20+40 = 70; high = 30+50+60 = 140
    assert out.loc["low", "weight"] == 70
    assert out.loc["high", "weight"] == 140


def test_alternate_weight_column():
    df = make_pums_df()
    out = compute_puma_joint(
        df, dimensions=["income_bin"], weight_col="PWGTP",
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    # Renters PWGTP: low = 5+7 = 12; high = 9
    assert out.loc["low", "weight"] == 12
    assert out.loc["high", "weight"] == 9


def test_nan_bin_drops_record():
    df = make_pums_df()
    df.loc[0, "income_bin"] = None
    out = compute_puma_joint(
        df, dimensions=["income_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    # Renter row 0 (WGTP=10) dropped → low = 20; high = 30
    assert out.loc["low", "weight"] == 20
    assert out.loc["high", "weight"] == 30


def test_nan_weight_drops_record():
    df = make_pums_df()
    df.loc[0, "WGTP"] = float("nan")
    out = compute_puma_joint(
        df, dimensions=["income_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    assert out.loc["low", "weight"] == 20


def test_empty_input_returns_empty_with_correct_index():
    out = compute_puma_joint(
        pd.DataFrame(columns=["income_bin", "age_bin", "WGTP"]),
        dimensions=["income_bin", "age_bin"],
    )
    assert out.empty
    assert list(out.index.names) == ["income_bin", "age_bin"]
    assert "weight" in out.columns


def test_all_filtered_out_returns_empty():
    df = make_pums_df()
    out = compute_puma_joint(
        df, dimensions=["income_bin"],
        filter_func=lambda d: d["TEN"] == 99,  # matches nothing
    )
    assert out.empty
    assert list(out.index.names) == ["income_bin"]


def test_dimensions_required():
    df = make_pums_df()
    with pytest.raises(ValueError, match="at least one column"):
        compute_puma_joint(df, dimensions=[])


# --- adapters ---

def test_joint_to_column_major_zero_fills():
    df = make_pums_df()
    joint = compute_puma_joint(
        df, dimensions=["income_bin", "age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    cm = joint_to_column_major(
        joint,
        row_dim="income_bin", col_dim="age_bin",
        row_order=["low", "high"],
        col_order=["young", "old"],
    )
    # Renters had only (low,young)=30 and (high,old)=30. The other two
    # combinations must be present and zero.
    assert cm == {
        "young": {"low": 30, "high": 0},
        "old":   {"low": 0,  "high": 30},
    }
    # Order preserved
    assert list(cm.keys()) == ["young", "old"]
    assert list(cm["young"].keys()) == ["low", "high"]


def test_joint_to_column_major_swapped_dim_assignment():
    # Swap which dim is rows vs columns; values must transpose accordingly.
    df = make_pums_df()
    joint = compute_puma_joint(
        df, dimensions=["income_bin", "age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    cm = joint_to_column_major(
        joint,
        row_dim="age_bin", col_dim="income_bin",
        row_order=["young", "old"],
        col_order=["low", "high"],
    )
    assert cm == {
        "low":  {"young": 30, "old": 0},
        "high": {"young": 0,  "old": 30},
    }


def test_column_major_to_joint_round_trip():
    df = make_pums_df()
    joint = compute_puma_joint(
        df, dimensions=["income_bin", "age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    cm = joint_to_column_major(
        joint, row_dim="income_bin", col_dim="age_bin",
        row_order=["low", "high"], col_order=["young", "old"],
    )
    back = column_major_to_joint(cm, "income_bin", "age_bin")
    # Round-trip: every populated cell in the original joint matches in `back`
    for (row_val, col_val), w in joint["weight"].items():
        assert int(back.loc[(row_val, col_val), "weight"]) == int(w)


def test_column_major_to_joint_empty_input():
    out = column_major_to_joint({}, "income_bin", "age_bin")
    assert out.empty
    assert list(out.index.names) == ["income_bin", "age_bin"]
    assert "weight" in out.columns


def test_joint_to_column_major_rejects_wrong_levels():
    df = make_pums_df()
    joint_1d = compute_puma_joint(df, dimensions=["income_bin"])
    with pytest.raises(ValueError, match="2 index levels"):
        joint_to_column_major(joint_1d, "income_bin", "age_bin",
                              ["low"], ["young"])


def test_joint_to_column_major_rejects_unknown_dim():
    df = make_pums_df()
    joint = compute_puma_joint(df, dimensions=["income_bin", "age_bin"])
    with pytest.raises(ValueError, match="not in index level"):
        joint_to_column_major(joint, "tenure", "age_bin",
                              ["low"], ["young", "old"])
