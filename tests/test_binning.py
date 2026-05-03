"""
Unit tests for telltalere_census.binning.bin_acs_row.

Exercises the generic bracket-to-bin collapse mechanism with a contrived
row (no live data), checking:
  - many-to-one bin mapping (multiple codes summing into one bin)
  - one-to-one mapping
  - suppressed cells (NaN) treated as 0
  - missing codes (not in row) treated as 0
  - stable output shape: every bin id appears in the result
  - row keys not referenced in code_to_bin are ignored
"""

from __future__ import annotations

import pandas as pd
import pytest

from telltalere_census.binning import bin_acs_row


def test_many_to_one_collapse():
    row = pd.Series({
        "A_001E": 10,
        "A_002E": 20,
        "A_003E": 30,
        "B_001E": 100,
    })
    code_to_bin = {
        "A_001E": "small",
        "A_002E": "small",
        "A_003E": "large",
    }
    out = bin_acs_row(row, code_to_bin, ["small", "large"])
    assert out == {"small": 30, "large": 30}


def test_suppressed_cell_treated_as_zero():
    row = pd.Series({
        "X_001E": 50,
        "X_002E": float("nan"),
        "X_003E": None,
    })
    code_to_bin = {
        "X_001E": "low",
        "X_002E": "low",
        "X_003E": "high",
    }
    out = bin_acs_row(row, code_to_bin, ["low", "high"])
    assert out == {"low": 50, "high": 0}


def test_missing_code_in_row():
    row = pd.Series({"A_001E": 5})
    code_to_bin = {"A_001E": "x", "A_002E": "x", "A_003E": "y"}
    out = bin_acs_row(row, code_to_bin, ["x", "y"])
    assert out == {"x": 5, "y": 0}


def test_all_bins_present_even_when_empty():
    row = pd.Series({"A_001E": 5})
    out = bin_acs_row(row, {"A_001E": "a"}, ["a", "b", "c"])
    assert set(out.keys()) == {"a", "b", "c"}
    assert out["b"] == 0
    assert out["c"] == 0


def test_unreferenced_row_keys_ignored():
    row = pd.Series({"A_001E": 5, "Z_999E": 9999})
    out = bin_acs_row(row, {"A_001E": "a"}, ["a"])
    assert out == {"a": 5}


def test_rounds_fractional_values():
    # ACS estimates are whole numbers in practice, but average-size tables
    # publish fractional. We round-half-to-even via Python's round(); this
    # documents the convention rather than asserting on the ambiguous .5 case.
    row = pd.Series({"A_001E": 10.4, "A_002E": 10.6})
    out = bin_acs_row(row, {"A_001E": "x", "A_002E": "x"}, ["x"])
    assert out == {"x": 10 + 11}


def test_empty_inputs_return_empty_bins():
    out = bin_acs_row(pd.Series({}, dtype=object), {}, ["a", "b"])
    assert out == {"a": 0, "b": 0}
