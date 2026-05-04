"""
Tests for income_tail.

Synthetic PUMS-shaped DataFrames cover decompose_high_income_tail under
varying tenure mixes, weight distributions, and sample sizes. Marginal
+ crosstab application tests verify the integer-sum invariants
explicitly. A live-PUMS regression test auto-skips when the Lake
County PUMS fixture is missing (Census endpoint outage during session
3 / 4 fixture generation).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from telltalere_census.income_tail import (
    DEFAULT_HIGH_INCOME_SUB_BRACKETS,
    apply_tail_decomposition,
    apply_tail_decomposition_to_crosstab,
    decompose_high_income_tail,
)


FIXTURES = Path(__file__).parent / "fixtures"
LAKE_PUMS = FIXTURES / "lake_county_pums.parquet"


# ---------------------------------------------------------------------------
# decompose_high_income_tail
# ---------------------------------------------------------------------------

def _make_pums(records: list[dict]) -> pd.DataFrame:
    """Materialize an explicit list-of-dicts as a PUMS-shaped frame."""
    return pd.DataFrame(records)


def test_decompose_basic_split():
    pums = _make_pums([
        {"TEN": 3, "WGTP": 100.0, "HINCP": 200_000},  # 150-250k
        {"TEN": 3, "WGTP": 200.0, "HINCP": 300_000},  # 250-350k
        {"TEN": 3, "WGTP": 100.0, "HINCP": 400_000},  # 350-500k
        {"TEN": 3, "WGTP": 100.0, "HINCP": 600_000},  # 500k+
        {"TEN": 3, "WGTP": 999.0, "HINCP":  50_000},  # below threshold, ignored
    ])
    res = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]),
        min_unweighted_n=4,  # exactly 4 records above threshold
    )
    assert res["n_unweighted_above_threshold"] == 4
    assert res["weighted_total_above_threshold"] == pytest.approx(500.0)
    assert res["shares"]["150_250k"] == pytest.approx(0.20)  # 100/500
    assert res["shares"]["250_350k"] == pytest.approx(0.40)  # 200/500
    assert res["shares"]["350_500k"] == pytest.approx(0.20)  # 100/500
    assert res["shares"]["500k_plus"] == pytest.approx(0.20) # 100/500
    assert sum(res["shares"].values()) == pytest.approx(1.0)
    assert res["sub_bracket_unweighted_n"] == {
        "150_250k": 1, "250_350k": 1, "350_500k": 1, "500k_plus": 1,
    }
    assert res["low_sample_flag"] is False  # 4 >= min_unweighted_n=4


def test_decompose_low_sample_flag():
    pums = _make_pums([
        {"TEN": 3, "WGTP": 50.0, "HINCP": 200_000},
        {"TEN": 3, "WGTP": 50.0, "HINCP": 300_000},
    ])
    res = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]),
        min_unweighted_n=50,
    )
    assert res["n_unweighted_above_threshold"] == 2
    assert res["low_sample_flag"] is True


def test_decompose_empty_input_zero_fills():
    """Per session 4 contract: empty result → shares all zero, no NaN."""
    pums = _make_pums([
        {"TEN": 3, "WGTP": 100.0, "HINCP": 50_000},  # below threshold
    ])
    res = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]),
    )
    assert res["n_unweighted_above_threshold"] == 0
    assert res["weighted_total_above_threshold"] == 0.0
    for label, _, _ in DEFAULT_HIGH_INCOME_SUB_BRACKETS:
        assert res["shares"][label] == 0.0
    assert res["low_sample_flag"] is True
    # No NaN anywhere
    assert all(not np.isnan(v) for v in res["shares"].values())


def test_decompose_owner_and_renter_filters_diverge():
    """Owner and renter give different shares for the same input — confirm
    the tenure_filter is actually applied."""
    pums = _make_pums([
        {"TEN": 1, "WGTP": 100.0, "HINCP": 200_000},  # owner
        {"TEN": 2, "WGTP": 100.0, "HINCP": 600_000},  # owner
        {"TEN": 3, "WGTP": 100.0, "HINCP": 200_000},  # renter
    ])
    owner = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([1, 2]), min_unweighted_n=2,
    )
    renter = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]), min_unweighted_n=1,
    )
    assert owner["shares"]["150_250k"] == pytest.approx(0.50)
    assert owner["shares"]["500k_plus"] == pytest.approx(0.50)
    assert renter["shares"]["150_250k"] == pytest.approx(1.00)
    assert renter["shares"]["500k_plus"] == pytest.approx(0.00)


def test_decompose_no_tenure_filter_uses_all_records():
    pums = _make_pums([
        {"TEN": 1, "WGTP": 100.0, "HINCP": 200_000},
        {"TEN": 3, "WGTP": 100.0, "HINCP": 300_000},
    ])
    res = decompose_high_income_tail(pums, tenure_filter=None, min_unweighted_n=1)
    assert res["n_unweighted_above_threshold"] == 2
    assert res["shares"]["150_250k"] == pytest.approx(0.50)
    assert res["shares"]["250_350k"] == pytest.approx(0.50)


def test_decompose_threshold_override():
    pums = _make_pums([
        {"TEN": 3, "WGTP": 100.0, "HINCP": 90_000},
        {"TEN": 3, "WGTP": 100.0, "HINCP": 150_000},
    ])
    # Threshold = $80k → both records qualify
    res = decompose_high_income_tail(
        pums, threshold=80_000.0,
        sub_brackets=[("80_120k", 80_000.0, 120_000.0),
                      ("120k_plus", 120_000.0, float("inf"))],
        min_unweighted_n=1,
    )
    assert res["n_unweighted_above_threshold"] == 2
    assert res["shares"]["80_120k"] == pytest.approx(0.50)
    assert res["shares"]["120k_plus"] == pytest.approx(0.50)


def test_decompose_uses_pwgtp_when_specified():
    pums = _make_pums([
        {"TEN": 3, "WGTP": 100.0, "PWGTP": 5.0,  "HINCP": 200_000},
        {"TEN": 3, "WGTP": 200.0, "PWGTP": 50.0, "HINCP": 300_000},
    ])
    # PWGTP-weighted: 5/55 vs 50/55
    res = decompose_high_income_tail(
        pums, weight_col="PWGTP", min_unweighted_n=1,
        tenure_filter=lambda d: d["TEN"].isin([3, 4]),
    )
    assert res["shares"]["150_250k"] == pytest.approx(5/55)
    assert res["shares"]["250_350k"] == pytest.approx(50/55)


def test_decompose_drops_records_with_missing_income_or_weight():
    pums = _make_pums([
        {"TEN": 3, "WGTP": 100.0, "HINCP": 200_000},
        {"TEN": 3, "WGTP": 100.0, "HINCP": float("nan")},  # dropped
        {"TEN": 3, "WGTP": float("nan"), "HINCP": 300_000},  # dropped
        {"TEN": 3, "WGTP": 100.0, "HINCP": 400_000},
    ])
    res = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]), min_unweighted_n=1,
    )
    assert res["n_unweighted_above_threshold"] == 2  # the two clean rows


# ---------------------------------------------------------------------------
# apply_tail_decomposition (1-D marginal)
# ---------------------------------------------------------------------------

def test_apply_marginal_preserves_integer_sum():
    marginal = {"under_35k": 120, "35_50k": 80, "50_75k": 100, "150k_plus": 100}
    shares = {"150_250k": 0.5, "250_350k": 0.3, "350_500k": 0.15, "500k_plus": 0.05}
    out = apply_tail_decomposition(marginal, shares)
    # 150k_plus removed, 4 sub-brackets added
    assert "150k_plus" not in out
    assert {"150_250k", "250_350k", "350_500k", "500k_plus"} <= set(out)
    # Per-sub-bracket allocation: 50, 30, 15, 5
    assert out["150_250k"] == 50
    assert out["250_350k"] == 30
    assert out["350_500k"] == 15
    assert out["500k_plus"] == 5
    # Total preserved exactly
    assert sum(out.values()) == sum(marginal.values())


def test_apply_marginal_handles_rounding_drift():
    # 100 * 0.333... cells will produce drift; allocator pushes drift to the
    # largest-share label so the sum is preserved exactly.
    marginal = {"150k_plus": 100}
    shares = {"a": 1/3, "b": 1/3, "c": 1/3}
    out = apply_tail_decomposition(marginal, shares)
    assert sum(out.values()) == 100


def test_apply_marginal_zero_top_count():
    marginal = {"under_35k": 50, "150k_plus": 0}
    shares = {"a": 0.5, "b": 0.5}
    out = apply_tail_decomposition(marginal, shares)
    assert out == {"under_35k": 50, "a": 0, "b": 0}


def test_apply_marginal_missing_top_label_raises():
    with pytest.raises(KeyError, match="not present in marginal"):
        apply_tail_decomposition({"under_35k": 10}, {"a": 1.0})


def test_apply_marginal_collision_raises():
    marginal = {"under_35k": 50, "150k_plus": 100}
    # New sub-bracket label collides with existing key
    shares = {"under_35k": 0.5, "other": 0.5}
    with pytest.raises(ValueError, match="collide"):
        apply_tail_decomposition(marginal, shares)


def test_apply_marginal_empty_shares_raises():
    with pytest.raises(ValueError, match="empty"):
        apply_tail_decomposition({"150k_plus": 10}, {})


# ---------------------------------------------------------------------------
# apply_tail_decomposition_to_crosstab (2-D)
# ---------------------------------------------------------------------------

def _make_crosstab(rows: list[tuple[str, str, int]],
                   row_dim: str = "income_bin",
                   col_dim: str = "age_bin") -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=[row_dim, col_dim, "weight"])
    return df.set_index([row_dim, col_dim])


def test_crosstab_preserves_per_other_column_sums():
    """The rake's column-sum invariant: each age column total must
    survive decomposition exactly."""
    crosstab = _make_crosstab([
        ("under_35k", "young", 30), ("under_35k", "old", 20),
        ("50_75k",    "young", 40), ("50_75k",    "old", 60),
        ("150k_plus", "young", 100), ("150k_plus", "old", 50),
    ])
    shares = {"150_250k": 0.4, "250_350k": 0.3, "350_500k": 0.2, "500k_plus": 0.1}
    out = apply_tail_decomposition_to_crosstab(crosstab, shares)

    # Original "150k_plus" rows are gone
    assert "150k_plus" not in set(out.index.get_level_values("income_bin"))
    # Each age column's sum must equal the input column's sum
    in_sums = crosstab.groupby("age_bin")["weight"].sum()
    out_sums = out.groupby("age_bin")["weight"].sum()
    for col in ["young", "old"]:
        assert out_sums[col] == in_sums[col], f"col {col} drifted"

    # Per-column allocation: 100 split as 40/30/20/10 in young; 50 split as 20/15/10/5 in old
    assert int(out.loc[("150_250k", "young"), "weight"]) == 40
    assert int(out.loc[("250_350k", "young"), "weight"]) == 30
    assert int(out.loc[("350_500k", "young"), "weight"]) == 20
    assert int(out.loc[("500k_plus", "young"), "weight"]) == 10
    assert int(out.loc[("150_250k", "old"), "weight"]) == 20
    assert int(out.loc[("250_350k", "old"), "weight"]) == 15
    assert int(out.loc[("350_500k", "old"), "weight"]) == 10
    assert int(out.loc[("500k_plus", "old"), "weight"]) == 5


def test_crosstab_per_column_drift_allocation():
    """Force per-column rounding drift; verify it's allocated to the
    largest-share label *within that column*, not globally."""
    crosstab = _make_crosstab([
        ("150k_plus", "young", 7),  # 7 * (1/3) cells will not round cleanly
        ("150k_plus", "old",   5),
    ])
    shares = {"a": 1/3, "b": 1/3, "c": 1/3}
    out = apply_tail_decomposition_to_crosstab(crosstab, shares)
    # Per-column sum invariant
    assert int(out.loc[("a", "young"), "weight"]) + \
           int(out.loc[("b", "young"), "weight"]) + \
           int(out.loc[("c", "young"), "weight"]) == 7
    assert int(out.loc[("a", "old"), "weight"]) + \
           int(out.loc[("b", "old"), "weight"]) + \
           int(out.loc[("c", "old"), "weight"]) == 5


def test_crosstab_handles_swapped_dimension_order():
    # Index level order is (age_bin, income_bin) — opposite of the default
    crosstab = _make_crosstab(
        [("young", "150k_plus", 80), ("old", "150k_plus", 40),
         ("young", "under_35k", 10), ("old", "under_35k", 20)],
        row_dim="age_bin", col_dim="income_bin",
    )
    shares = {"150_250k": 0.5, "500k_plus": 0.5}
    out = apply_tail_decomposition_to_crosstab(crosstab, shares)
    # Per-age sum invariant
    assert out.loc[("young", "150_250k"), "weight"] + \
           out.loc[("young", "500k_plus"), "weight"] == 80
    assert out.loc[("old", "150_250k"), "weight"] + \
           out.loc[("old", "500k_plus"), "weight"] == 40
    # Untouched rows pass through
    assert int(out.loc[("young", "under_35k"), "weight"]) == 10


def test_crosstab_collision_raises():
    crosstab = _make_crosstab([
        ("under_35k", "young", 10),
        ("150k_plus", "young", 100),
    ])
    # "under_35k" already in income dim; cannot also be a sub-bracket label
    shares = {"under_35k": 0.5, "other": 0.5}
    with pytest.raises(ValueError, match="collide"):
        apply_tail_decomposition_to_crosstab(crosstab, shares)


def test_crosstab_invalid_index_levels_raises():
    df = pd.DataFrame({"weight": [1, 2, 3]}, index=pd.Index(["a", "b", "c"], name="x"))
    with pytest.raises(ValueError, match="2 index levels"):
        apply_tail_decomposition_to_crosstab(df, {"sub": 1.0})


def test_crosstab_unknown_income_dim_raises():
    crosstab = _make_crosstab([("a", "b", 1)])
    with pytest.raises(ValueError, match="not in the crosstab index"):
        apply_tail_decomposition_to_crosstab(crosstab, {"sub": 1.0}, income_dim="not_there")


# ---------------------------------------------------------------------------
# Live-PUMS regression — auto-skipped when fixture missing
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not LAKE_PUMS.exists(),
    reason="Lake County PUMS fixture missing — Census PUMS endpoint outage. "
           "Re-run scripts/generate_lake_county_fixtures.py.",
)
def test_decompose_lake_county_renter_realistic_shape():
    """Sanity check on real PUMS data: shares sum to 1.0, low_sample_flag
    sensible for a metro-scale county, sub-bracket counts non-negative."""
    pums = pd.read_parquet(LAKE_PUMS)
    res = decompose_high_income_tail(
        pums, tenure_filter=lambda d: d["TEN"].isin([3, 4]),
    )
    assert sum(res["shares"].values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v >= 0 for v in res["shares"].values())
    # Lake County is large enough that the renter tail should clear sample-min
    assert res["n_unweighted_above_threshold"] >= 50, (
        f"Lake County renter $150k+ records below 50: {res['n_unweighted_above_threshold']}"
    )
    assert res["low_sample_flag"] is False
