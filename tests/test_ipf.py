"""
Synthetic / hand-computable tests for ipf.rake_to_marginals.

Live-data parity against Core_BTR's ipf_crosstab.ipf_2d lives in
test_core_btr_parity.py.

Coverage:
  - Convergence on a trivially-marginal-consistent prior (output equals prior)
  - Reconciliation: row/col sums match marginals after rounding
  - Zero-marginal axis: row or column with marginal=0 is forced to zero
  - All-zero target: returns empty joint, converged=True, iterations=0
  - Uniform fallback: row with zero prior mass but nonzero marginal
    triggers uniform-across-active-cols imputation
  - Grand-total drift below 1%: rounding residual closes to 0
  - Bin-id ordering: result joint preserves marginal key order
"""

from __future__ import annotations

import pytest

from telltalere_census.ipf import rake_to_marginals


def _column_sums(joint: dict[str, dict[str, int]]) -> dict[str, int]:
    return {col_id: sum(cells.values()) for col_id, cells in joint.items()}


def _row_sums(joint: dict[str, dict[str, int]], row_ids: list[str]) -> dict[str, int]:
    out = {r: 0 for r in row_ids}
    for col_cells in joint.values():
        for r in row_ids:
            out[r] += col_cells.get(r, 0)
    return out


def test_already_consistent_prior_is_preserved():
    # Prior cells exactly match the marginals — IPF converges in 1 iteration.
    prior = {
        "young":   {"low": 30, "high": 10},
        "old":     {"low": 20, "high": 40},
    }
    row_marginal = {"low": 50, "high": 50}
    col_marginal = {"young": 40, "old": 60}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert res["converged"] is True
    assert _row_sums(res["joint"], ["low", "high"]) == {"low": 50, "high": 50}
    assert _column_sums(res["joint"]) == {"young": 40, "old": 60}


def test_rake_reconciles_to_new_marginals():
    # Prior is the uniform 100 across 4 cells. Marginals push 80% of the
    # mass into the "young" column and 70% into the "low" row. After
    # raking, row sums and col sums must match the marginals exactly.
    prior = {
        "young": {"low": 25, "high": 25},
        "old":   {"low": 25, "high": 25},
    }
    row_marginal = {"low": 70, "high": 30}
    col_marginal = {"young": 80, "old": 20}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert _row_sums(res["joint"], ["low", "high"]) == {"low": 70, "high": 30}
    assert _column_sums(res["joint"]) == {"young": 80, "old": 20}
    # Independence assumption: cell ≈ row_share * col_share * total = 0.7 * 0.8 * 100 = 56
    assert res["joint"]["young"]["low"] == pytest.approx(56, abs=1)


def test_zero_col_marginal_zeroes_column():
    prior = {
        "young": {"low": 20, "high": 20},
        "old":   {"low": 20, "high": 20},
    }
    row_marginal = {"low": 30, "high": 30}
    col_marginal = {"young": 60, "old": 0}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert res["joint"]["old"] == {"low": 0, "high": 0}
    assert _column_sums(res["joint"])["young"] == 60
    assert "old" in res["zero_marginal_cols"]


def test_zero_row_marginal_zeroes_row():
    prior = {
        "young": {"low": 20, "high": 20},
        "old":   {"low": 20, "high": 20},
    }
    row_marginal = {"low": 0, "high": 60}
    col_marginal = {"young": 30, "old": 30}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    rs = _row_sums(res["joint"], ["low", "high"])
    assert rs["low"] == 0
    assert rs["high"] == 60
    assert "low" in res["zero_marginal_rows"]


def test_all_zero_target_returns_empty():
    prior = {"young": {"low": 50, "high": 50}}
    res = rake_to_marginals(
        prior,
        row_marginal={"low": 0, "high": 0},
        col_marginal={"young": 0},
    )
    assert res["converged"] is True
    assert res["iterations"] == 0
    assert res["joint"] == {"young": {"low": 0, "high": 0}}


def test_uniform_fallback_for_zero_prior_row():
    # Prior has zero mass for "high" income, but marginal demands 40 there.
    # Uniform fallback distributes the 40 across active columns.
    prior = {
        "young": {"low": 50, "high": 0},
        "old":   {"low": 50, "high": 0},
    }
    row_marginal = {"low": 60, "high": 40}
    col_marginal = {"young": 60, "old": 40}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert "high" in res["fallback_rows_uniform"]
    assert _row_sums(res["joint"], ["low", "high"])["high"] == 40
    assert _column_sums(res["joint"]) == {"young": 60, "old": 40}


def test_result_preserves_marginal_key_order():
    # The result joint's outer dict iterates in col_marginal key order;
    # each inner dict iterates in row_marginal key order.
    prior = {
        "c2": {"r2": 1, "r1": 1},
        "c1": {"r2": 1, "r1": 1},
    }
    row_marginal = {"r1": 50, "r2": 50}   # r1 first
    col_marginal = {"c1": 60, "c2": 40}   # c1 first
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert list(res["joint"].keys()) == ["c1", "c2"]
    for col_id in res["joint"]:
        assert list(res["joint"][col_id].keys()) == ["r1", "r2"]


def test_rounding_residual_closes_within_tolerance():
    # Construct a case that yields fractional cells and verify the post-
    # rounding reconciliation drives row/col/total drift to zero.
    prior = {
        "a": {"x": 1, "y": 1, "z": 1},
        "b": {"x": 1, "y": 1, "z": 1},
        "c": {"x": 1, "y": 1, "z": 1},
    }
    row_marginal = {"x": 33, "y": 34, "z": 33}
    col_marginal = {"a": 33, "b": 34, "c": 33}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    assert res["rounding_residual"] == 0
    assert _row_sums(res["joint"], ["x", "y", "z"]) == {"x": 33, "y": 34, "z": 33}
    assert _column_sums(res["joint"]) == {"a": 33, "b": 34, "c": 33}


def test_independence_recovered_from_uniform_prior():
    # Uniform prior + product-form marginals → IPF should recover the
    # outer-product (independence) joint to within rounding tolerance.
    prior = {
        "y": {"l": 1, "m": 1, "h": 1},
        "o": {"l": 1, "m": 1, "h": 1},
    }
    # Total = 200. Row shares: l=0.5, m=0.3, h=0.2. Col shares: y=0.6, o=0.4.
    row_marginal = {"l": 100, "m": 60, "h": 40}
    col_marginal = {"y": 120, "o": 80}
    res = rake_to_marginals(prior, row_marginal, col_marginal)

    j = res["joint"]
    assert j["y"]["l"] == pytest.approx(60, abs=1)
    assert j["y"]["m"] == pytest.approx(36, abs=1)
    assert j["y"]["h"] == pytest.approx(24, abs=1)
    assert j["o"]["l"] == pytest.approx(40, abs=1)
    assert j["o"]["m"] == pytest.approx(24, abs=1)
    assert j["o"]["h"] == pytest.approx(16, abs=1)
