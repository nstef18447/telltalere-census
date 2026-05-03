"""
Unit tests for telltalere_census.bracket_allocation.proportional_from_brackets.

Hand-computable contrived cases covering:
  - Interior interval: target falls fully inside one bracket
  - Multi-bracket interval: target spans several closed brackets
  - Partial-bracket overlap: target slices through bracket edges
  - Top-bracket inclusion: target_high >= top_floor pulls in the full top count
  - Top-bracket exclusion: target_high < top_floor leaves it out
  - Suppressed (NaN) bracket cell treated as 0 (does not poison denominator)
  - Missing / zero denominator returns None
  - Share > 1.0 + epsilon raises RuntimeError (bracket-enumeration sanity)

Bracket scheme used in tests (B25118-style income brackets with $150k+ top):
    [0, 5k), [5k, 10k), ..., [100k, 150k), top: [150k, +inf)
Total denominator = 100. With known per-bracket counts, target intervals
have hand-checkable expected shares.
"""

from __future__ import annotations

import pandas as pd
import pytest

from telltalere_census.bracket_allocation import proportional_from_brackets


CLOSED_INCOME = [
    ("B_015E", 0,        5_000),
    ("B_016E", 5_000,   10_000),
    ("B_017E", 10_000,  15_000),
    ("B_018E", 15_000,  20_000),
    ("B_019E", 20_000,  25_000),
    ("B_020E", 25_000,  35_000),
    ("B_021E", 35_000,  50_000),
    ("B_022E", 50_000,  75_000),
    ("B_023E", 75_000, 100_000),
    ("B_024E", 100_000, 150_000),
]
TOP_CODE = "B_025E"
TOP_FLOOR = 150_000.0
DENOM = "B_014E"


def make_row(counts: dict[str, float], denom: float = 100.0) -> pd.Series:
    base = {code: 0 for code, _, _ in CLOSED_INCOME}
    base[TOP_CODE] = 0
    base[DENOM] = denom
    base.update(counts)
    return pd.Series(base)


def test_interior_full_bracket():
    # 10 households in the [50k, 75k) bracket; target [50k, 75k) → 10/100 = 0.10
    row = make_row({"B_022E": 10})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=75_000,
    )
    assert share == pytest.approx(0.10)


def test_multi_bracket_span():
    # 10 in [50,75), 8 in [75,100), target [50k, 100k) → 18/100 = 0.18
    row = make_row({"B_022E": 10, "B_023E": 8})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=100_000,
    )
    assert share == pytest.approx(0.18)


def test_partial_bracket_overlap():
    # 20 households in [50k, 75k); target [60k, 75k) overlaps half (15/25)
    # → contribution = 20 * (15/25) = 12 → 12/100 = 0.12
    row = make_row({"B_022E": 20})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=60_000, target_high=75_000,
    )
    assert share == pytest.approx(0.12)


def test_top_bracket_included_when_target_reaches_floor():
    # 15 in top, target [200k, 500k) → top_floor=150k <= target_high=500k → full 15
    row = make_row({TOP_CODE: 15})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=200_000, target_high=500_000,
    )
    assert share == pytest.approx(0.15)


def test_top_bracket_excluded_when_target_below_floor():
    # 15 in top + 10 in [100k, 150k); target [50k, 100k) sees only [50k, 75k) and [75k, 100k)
    # → counts 0 + 0 = 0 (those brackets weren't populated). Top excluded
    # because target_high=100k < top_floor=150k.
    row = make_row({TOP_CODE: 15, "B_024E": 10})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=100_000,
    )
    assert share == pytest.approx(0.0)


def test_target_high_equals_top_floor_includes_top():
    # Boundary: target_high == top_floor → top bracket included (>= comparison)
    row = make_row({TOP_CODE: 25, "B_024E": 50})  # 25 + 50 in covered ranges
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=100_000, target_high=150_000,
    )
    # [100k, 150k) closed bracket: 50 fully overlapped → 50.
    # Top bracket [150k, +inf): target_high=150k == top_floor → included → +25.
    assert share == pytest.approx(0.75)


def test_suppressed_cell_treated_as_zero():
    # Suppressed B_022E (NaN) and a populated B_023E. The NaN must not
    # poison the share — it counts as 0 contribution from that bracket.
    row = make_row({"B_022E": float("nan"), "B_023E": 8})
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=100_000,
    )
    assert share == pytest.approx(0.08)


def test_missing_denominator_returns_none():
    row = make_row({"B_022E": 10}, denom=float("nan"))
    assert proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=75_000,
    ) is None


def test_zero_denominator_returns_none():
    row = make_row({"B_022E": 10}, denom=0)
    assert proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=75_000,
    ) is None


def test_share_above_one_raises():
    # Bracket counts engineered to exceed denom — caller bug. Should raise.
    row = make_row({"B_022E": 60, "B_023E": 60}, denom=100)
    with pytest.raises(RuntimeError, match="Bracket-enumeration bug"):
        proportional_from_brackets(
            row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
            target_low=50_000, target_high=100_000,
        )


def test_share_clamped_to_one_within_epsilon():
    # If the sum lands a hair above 1 due to float noise (within 1e-9), it
    # should clamp to exactly 1.0 rather than raise. Pre-1e-9 floor passes
    # through clean.
    row = make_row({"B_022E": 100}, denom=100)
    share = proportional_from_brackets(
        row, DENOM, CLOSED_INCOME, TOP_CODE, TOP_FLOOR,
        target_low=50_000, target_high=75_000,
    )
    assert share == 1.0
