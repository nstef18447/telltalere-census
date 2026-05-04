"""Unit tests for telltalere_census.trends."""

from __future__ import annotations

import math

import pytest

from telltalere_census.trends import (
    TrendResult,
    cagr,
    compute_trend_metrics,
    cumulative_trend,
    is_change_significant,
)


# ---------------------------------------------------------------------------
# cagr
# ---------------------------------------------------------------------------

def test_cagr_doubling_over_5_years():
    # 2x growth over 5 years -> 1.1487 ≈ 14.87%
    result = cagr(100.0, 200.0, 5)
    assert result is not None
    assert abs(result - 0.1487) < 1e-3


def test_cagr_no_change():
    assert cagr(100.0, 100.0, 5) == 0.0


def test_cagr_halving():
    # 0.5x over 5 years -> negative CAGR
    result = cagr(100.0, 50.0, 5)
    assert result is not None
    assert abs(result - (-0.1294)) < 1e-3


def test_cagr_late_value_zero():
    # 100% loss per year is a valid CAGR result of -1.0
    assert cagr(100.0, 0.0, 5) == -1.0


def test_cagr_returns_none_for_negative_early():
    assert cagr(-50.0, 100.0, 5) is None


def test_cagr_returns_none_for_zero_early():
    assert cagr(0.0, 100.0, 5) is None


def test_cagr_returns_none_for_negative_late():
    assert cagr(100.0, -50.0, 5) is None


def test_cagr_returns_none_for_zero_years():
    assert cagr(100.0, 200.0, 0) is None


def test_cagr_returns_none_for_negative_years():
    assert cagr(100.0, 200.0, -1) is None


def test_cagr_returns_none_for_nan_inputs():
    assert cagr(float("nan"), 100.0, 5) is None
    assert cagr(100.0, float("nan"), 5) is None


# ---------------------------------------------------------------------------
# is_change_significant
# ---------------------------------------------------------------------------

def test_significant_combined_clearly_above():
    # diff = 50, combined = sqrt(100 + 100) = 14.14
    assert is_change_significant(100.0, 10.0, 150.0, 10.0, method="moe_combined")


def test_significant_combined_clearly_below():
    # diff = 5, combined = sqrt(100 + 100) = 14.14
    assert not is_change_significant(100.0, 10.0, 105.0, 10.0, method="moe_combined")


def test_significant_overlap_no_overlap():
    # CIs: [90,110] vs [140,160] — no overlap
    assert is_change_significant(100.0, 10.0, 150.0, 10.0, method="moe_overlap")


def test_significant_overlap_with_overlap():
    # CIs: [90,110] vs [105,115] — overlap at 105-110
    assert not is_change_significant(100.0, 10.0, 110.0, 5.0, method="moe_overlap")


def test_significant_overlap_touching_is_not_significant():
    # CIs: [90,110] vs [110,130] — touch at 110, not strictly disjoint
    assert not is_change_significant(100.0, 10.0, 120.0, 10.0, method="moe_overlap")


def test_significant_combined_strictly_tighter_than_overlap():
    # Construct a case where combined says significant but overlap says not.
    # diff = 20, MOEs both 12. combined = sqrt(144+144) = 16.97 -> 20 > 16.97 SIG
    # overlap: CIs [88,112] vs [108,132] overlap 108-112 -> NOT SIG
    assert is_change_significant(100.0, 12.0, 120.0, 12.0, method="moe_combined")
    assert not is_change_significant(100.0, 12.0, 120.0, 12.0, method="moe_overlap")


def test_significant_returns_false_on_nan():
    assert not is_change_significant(float("nan"), 10.0, 100.0, 10.0)
    assert not is_change_significant(100.0, float("nan"), 100.0, 10.0)


def test_significant_unknown_method_raises():
    with pytest.raises(ValueError, match="unknown significance method"):
        is_change_significant(100.0, 10.0, 150.0, 10.0, method="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_trend_metrics
# ---------------------------------------------------------------------------

def test_compute_trend_basic_increasing():
    result = compute_trend_metrics({2018: 1000.0, 2023: 1200.0})
    assert result["early_vintage"] == 2018
    assert result["late_vintage"] == 2023
    assert result["years_elapsed"] == 5
    assert result["absolute_change"] == 200.0
    assert result["percent_change"] == pytest.approx(0.20)
    assert result["cagr"] == pytest.approx(0.0371, abs=1e-3)
    assert result["direction"] == "increasing"
    assert result["is_significant"] is False  # No MOEs provided
    assert result["moe_combined"] is None


def test_compute_trend_decreasing():
    result = compute_trend_metrics({2018: 1000.0, 2023: 800.0})
    assert result["direction"] == "decreasing"
    assert result["percent_change"] == pytest.approx(-0.20)


def test_compute_trend_flat_with_significant_moe():
    # 2% change, MOE pair tight — flat (small significant change)
    result = compute_trend_metrics(
        {2018: 1000.0, 2023: 1020.0},
        moes_by_vintage={2018: 5.0, 2023: 5.0},
    )
    # combined moe = sqrt(50) ~ 7.07; diff = 20 > 7.07 -> significant
    # |pct| = 0.02 < 0.05 threshold AND significant -> "flat"
    assert result["is_significant"] is True
    assert result["direction"] == "flat"


def test_compute_trend_noise_with_overlapping_moe():
    # 2% change, MOE pair wide — noise
    result = compute_trend_metrics(
        {2018: 1000.0, 2023: 1020.0},
        moes_by_vintage={2018: 100.0, 2023: 100.0},
    )
    # combined = sqrt(20000) ~ 141; diff = 20 < 141 -> not significant
    assert result["is_significant"] is False
    assert result["direction"] == "noise"


def test_compute_trend_intermediate_vintages_ignored():
    # 2018 = 1000, 2023 = 1500, but 2020 = 50000 (anomaly) — 2020 ignored
    result = compute_trend_metrics({2018: 1000.0, 2020: 50000.0, 2023: 1500.0})
    assert result["early_vintage"] == 2018
    assert result["late_vintage"] == 2023
    assert result["absolute_change"] == 500.0


def test_compute_trend_zero_early_returns_none_pct():
    result = compute_trend_metrics({2018: 0.0, 2023: 100.0})
    assert result["percent_change"] is None
    assert result["cagr"] is None
    # No MOEs, not significant -> direction == "noise" (no pct signal)
    assert result["direction"] == "noise"


def test_compute_trend_negative_early_returns_none_cagr():
    # Net income variables can be negative in PUMS, though not in summary
    # tables. Still should not blow up.
    result = compute_trend_metrics({2018: -100.0, 2023: 200.0})
    assert result["cagr"] is None
    # -100 to 200: pct = (200 - (-100)) / -100 = -3.0 → < -0.05 → decreasing
    assert result["direction"] == "decreasing"


def test_compute_trend_raises_on_single_vintage():
    with pytest.raises(ValueError, match=">=2 vintages"):
        compute_trend_metrics({2018: 100.0})


def test_compute_trend_raises_on_empty():
    with pytest.raises(ValueError, match=">=2 vintages"):
        compute_trend_metrics({})


def test_compute_trend_raises_on_nan_endpoint():
    with pytest.raises(ValueError, match="null/NaN"):
        compute_trend_metrics({2018: float("nan"), 2023: 100.0})


def test_compute_trend_moe_present_but_nan_treated_as_missing():
    # A MOE that is NaN should not crash; should be treated as "no MOE signal"
    result = compute_trend_metrics(
        {2018: 1000.0, 2023: 1200.0},
        moes_by_vintage={2018: float("nan"), 2023: 5.0},
    )
    assert result["is_significant"] is False
    assert result["moe_combined"] is None


def test_compute_trend_typed_dict_fields():
    """The returned dict has all TrendResult fields populated."""
    result = compute_trend_metrics({2018: 1000.0, 2023: 1200.0})
    expected_keys = set(TrendResult.__annotations__.keys())
    assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# cumulative_trend
# ---------------------------------------------------------------------------

def test_cumulative_trend_n_minus_1_results():
    values = {2018: 100.0, 2019: 110.0, 2020: 105.0, 2021: 120.0, 2022: 130.0}
    results = cumulative_trend(values)
    assert len(results) == 4  # N-1


def test_cumulative_trend_consecutive_pairs():
    values = {2018: 100.0, 2019: 110.0, 2020: 105.0}
    results = cumulative_trend(values)
    assert results[0]["early_vintage"] == 2018
    assert results[0]["late_vintage"] == 2019
    assert results[1]["early_vintage"] == 2019
    assert results[1]["late_vintage"] == 2020


def test_cumulative_trend_directions_per_pair():
    values = {2018: 100.0, 2019: 200.0, 2020: 200.0}
    results = cumulative_trend(values)
    assert results[0]["direction"] == "increasing"
    # 2019 -> 2020: no change. No MOEs supplied, is_sig=False, direction="noise"
    assert results[1]["direction"] == "noise"
    assert results[1]["absolute_change"] == 0.0


def test_cumulative_trend_propagates_moes_per_pair():
    values = {2018: 100.0, 2019: 110.0, 2020: 120.0}
    moes = {2018: 5.0, 2019: 5.0, 2020: 5.0}
    results = cumulative_trend(values, moes_by_vintage=moes)
    # Each pair-result should have moe_combined populated
    for r in results:
        assert r["moe_combined"] is not None
        assert r["moe_combined"] == pytest.approx(math.sqrt(50))


def test_cumulative_trend_raises_on_single_vintage():
    with pytest.raises(ValueError, match=">=2 vintages"):
        cumulative_trend({2018: 100.0})


def test_cumulative_trend_handles_missing_moe_for_some_years():
    # If a year has values but no MOE, that pair returns is_significant=False
    # and moe_combined=None for the affected pair
    values = {2018: 100.0, 2019: 110.0, 2020: 120.0}
    moes = {2018: 5.0, 2020: 5.0}  # 2019 missing
    results = cumulative_trend(values, moes_by_vintage=moes)
    # Pair (2018, 2019): 2019 MOE missing -> not significant
    assert results[0]["is_significant"] is False
    # Pair (2019, 2020): 2019 MOE missing -> not significant
    assert results[1]["is_significant"] is False
