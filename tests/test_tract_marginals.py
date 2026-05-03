"""
Unit tests for telltalere_census.tract_marginals.

Contrived ACS-row inputs only; live-data parity tests against Core_BTR
output live in test_core_btr_parity.py.

Coverage:
  - compute_tract_marginal: many-to-one and one-to-one mappings, suppressed
    cells, missing codes, stable output shape
  - read_authoritative_total: integer round-trip, NA handling
  - subtotal_moe_flagged: above/below threshold, undefined-CV cases
    (zero estimate, missing MOE, missing estimate), custom threshold
"""

from __future__ import annotations

import pandas as pd
import pytest

from telltalere_census.tract_marginals import (
    BracketConfig,
    compute_tract_marginal,
    read_authoritative_total,
    subtotal_moe_flagged,
)


# Renter-style income config for tests — mirrors the BTR mapping shape
# without baking in the ACS B25118 codes themselves.
INCOME_CONFIG: BracketConfig = {
    "denominator_var": "B_014E",
    "bracket_to_bin": {
        "B_015E": "under_35k",
        "B_016E": "under_35k",
        "B_017E": "under_35k",
        "B_018E": "under_35k",
        "B_019E": "under_35k",
        "B_020E": "under_35k",
        "B_021E": "35_50k",
        "B_022E": "50_75k",
        "B_023E": "75_100k",
        "B_024E": "100_150k",
        "B_025E": "150k_plus",
    },
    "bin_order": [
        "under_35k", "35_50k", "50_75k", "75_100k", "100_150k", "150k_plus",
    ],
}


def test_collapse_to_display_bins():
    row = pd.Series({
        "B_014E": 100,                                # subtotal
        "B_015E": 5,  "B_016E": 5,  "B_017E": 5,
        "B_018E": 5,  "B_019E": 5,  "B_020E": 5,    # 30 → "under_35k"
        "B_021E": 10,                                # → "35_50k"
        "B_022E": 20,                                # → "50_75k"
        "B_023E": 15,                                # → "75_100k"
        "B_024E": 15,                                # → "100_150k"
        "B_025E": 10,                                # → "150k_plus"
    })
    out = compute_tract_marginal(row, INCOME_CONFIG)
    assert out == {
        "under_35k": 30,
        "35_50k":   10,
        "50_75k":   20,
        "75_100k":  15,
        "100_150k": 15,
        "150k_plus": 10,
    }
    # Every bin id appears in stable order
    assert list(out.keys()) == INCOME_CONFIG["bin_order"]


def test_suppressed_cells_treated_as_zero():
    row = pd.Series({
        "B_021E": float("nan"),
        "B_022E": None,
        "B_023E": 7,
    })
    out = compute_tract_marginal(row, INCOME_CONFIG)
    assert out["35_50k"] == 0
    assert out["50_75k"] == 0
    assert out["75_100k"] == 7
    assert out["under_35k"] == 0


def test_missing_codes_treated_as_zero():
    row = pd.Series({"B_021E": 4})
    out = compute_tract_marginal(row, INCOME_CONFIG)
    # No other bracket cells in the row → all bins zero except 35_50k
    assert out["35_50k"] == 4
    for bin_id in ("under_35k", "50_75k", "75_100k", "100_150k", "150k_plus"):
        assert out[bin_id] == 0


def test_authoritative_total_round_trip():
    row = pd.Series({"B25003_003E": 258_455})
    assert read_authoritative_total(row, "B25003_003E") == 258_455


def test_authoritative_total_none_on_suppression():
    row = pd.Series({"B25003_003E": float("nan")})
    assert read_authoritative_total(row, "B25003_003E") is None


def test_authoritative_total_none_on_missing_var():
    row = pd.Series({"OTHER": 1})
    assert read_authoritative_total(row, "B25003_003E") is None


# --- subtotal_moe_flagged ---

def test_moe_flag_below_threshold():
    # CV = 100 / 1000 = 0.10 → not flagged
    row = pd.Series({"B_014E": 1000, "B_014M": 100})
    assert subtotal_moe_flagged(row, "B_014E") is False


def test_moe_flag_above_threshold():
    # CV = 500 / 1000 = 0.50 → flagged at default 0.40
    row = pd.Series({"B_014E": 1000, "B_014M": 500})
    assert subtotal_moe_flagged(row, "B_014E") is True


def test_moe_flag_exactly_at_threshold_not_flagged():
    # CV == threshold → not flagged (strictly greater rule)
    row = pd.Series({"B_014E": 1000, "B_014M": 400})
    assert subtotal_moe_flagged(row, "B_014E") is False


def test_moe_flag_zero_estimate_undefined_does_not_flag():
    row = pd.Series({"B_014E": 0, "B_014M": 50})
    assert subtotal_moe_flagged(row, "B_014E") is False


def test_moe_flag_missing_moe_does_not_flag():
    row = pd.Series({"B_014E": 1000})
    assert subtotal_moe_flagged(row, "B_014E") is False


def test_moe_flag_missing_estimate_does_not_flag():
    row = pd.Series({"B_014M": 100})
    assert subtotal_moe_flagged(row, "B_014E") is False


def test_moe_flag_custom_threshold():
    # CV = 0.20 → not flagged at 0.40, but flagged at 0.10
    row = pd.Series({"B_014E": 1000, "B_014M": 200})
    assert subtotal_moe_flagged(row, "B_014E", cv_threshold=0.40) is False
    assert subtotal_moe_flagged(row, "B_014E", cv_threshold=0.10) is True
