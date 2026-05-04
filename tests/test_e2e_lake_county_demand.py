"""
End-to-end integration test: full demand-synthesis pipeline for Lake County, IL.

Composes every primitive in the package:

    compute_tract_marginal           (renter + owner, income/age/hh_size)
    read_authoritative_total          (B25003_003E renter, B25003_002E owner)
    compute_puma_joint               (income x age, both tenures)
    joint_to_column_major            (DataFrame -> dict-of-dicts adapter)
    rake_to_marginals                (IPF, both tenures)
    column_major_to_joint            (dict-of-dicts -> DataFrame adapter)
    decompose_high_income_tail       (PUMS-derived shares)
    apply_tail_decomposition_to_crosstab  (replaces 150k_plus row)
    aggregate_to_lifestages_with_tail     (rollup)

Two variants in the same file:
  - test_e2e_synthetic_pums (always runs): synthesizes a small PUMS-shaped
    DataFrame and exercises the full composition. Proves API correctness.
  - test_e2e_live_pums (auto-skip): uses the real Lake County PUMS fixture
    when present. Proves real-data behavior.

Marginal-sum invariants asserted on every tract:
  - Renter income marginal sum == B25003_003E (within bracket-suppression tolerance)
  - Owner  income marginal sum == B25003_002E (within bracket-suppression tolerance)
  - Post-rake renter crosstab column sums == age marginal exactly
  - Post-rake owner  crosstab column sums == age marginal exactly
  - Tail-decomposed crosstab column sums preserved exactly
  - Lifestage rollup total == post-rake renter total (within rounding)
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import pytest

from telltalere_census import (
    column_major_to_joint,
    compute_puma_joint,
    compute_tract_marginal,
    joint_to_column_major,
    rake_to_marginals,
    read_authoritative_total,
)
from telltalere_census.income_tail import (
    apply_tail_decomposition_to_crosstab,
    decompose_high_income_tail,
)
from telltalere_census.lifestage import (
    RCLCO_DEFAULT_LIFESTAGE_GRID,
    aggregate_to_lifestages_with_tail,
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from _btr_configs import (  # noqa: E402  — sys.path injection above
    AGE_BIN_IDS,
    AGE_OWNER_CONFIG,
    AGE_RENTER_CONFIG,
    INCOME_BIN_IDS,
    INCOME_OWNER_CONFIG,
    INCOME_RENTER_CONFIG,
    bin_age,
    bin_income,
)


FIXTURES = Path(__file__).parent / "fixtures"
LAKE_TRACTS = FIXTURES / "lake_county_tracts.parquet"
LAKE_PUMS = FIXTURES / "lake_county_pums.parquet"


# ---------------------------------------------------------------------------
# Pipeline helper — same code path under both fixture sources
# ---------------------------------------------------------------------------

def _run_pipeline_one_tract(
    tract_row: pd.Series,
    pums: pd.DataFrame,
    tenure_filter: Callable[[pd.DataFrame], pd.Series],
    income_config,
    age_config,
    authoritative_total_var: str,
) -> dict:
    """
    Run the full demand-synthesis pipeline for one (tract, tenure) combination.

    Returns a dict bundling every intermediate so callers can assert
    invariants explicitly.
    """
    # 1. Tract marginals from ACS bracket rollups
    income_m = compute_tract_marginal(tract_row, income_config)
    age_m = compute_tract_marginal(tract_row, age_config)
    auth_total = read_authoritative_total(tract_row, authoritative_total_var)

    # 2. PUMS pre-binning + filtering -> PUMA joint
    pre_binned = pums.copy()
    pre_binned["_income_bin"] = pre_binned["HINCP"].apply(bin_income)
    pre_binned["_age_bin"] = pre_binned["AGEP"].apply(bin_age)

    joint_df = compute_puma_joint(
        pre_binned,
        dimensions=["_income_bin", "_age_bin"],
        filter_func=tenure_filter,
    )
    prior = joint_to_column_major(
        joint_df,
        row_dim="_income_bin", col_dim="_age_bin",
        row_order=INCOME_BIN_IDS, col_order=AGE_BIN_IDS,
    )

    # 3. Rake the PUMA prior to the tract marginals
    raked = rake_to_marginals(prior, income_m, age_m)
    raked_df = column_major_to_joint(raked["joint"], "income_bin", "age_bin")

    # 4. PUMS-derived tail decomposition shares; apply to the raked crosstab
    tail = decompose_high_income_tail(
        pre_binned, tenure_filter=tenure_filter, min_unweighted_n=20,
    )
    if tail["weighted_total_above_threshold"] > 0:
        decomposed = apply_tail_decomposition_to_crosstab(
            raked_df, tail["shares"], top_bracket_label="150k_plus",
        )
    else:
        decomposed = raked_df

    # 5. Lifestage rollup (roll the tail back into 150k_plus before applying
    # the standard 6-bin grid).
    lifestages = aggregate_to_lifestages_with_tail(
        decomposed, RCLCO_DEFAULT_LIFESTAGE_GRID, tail_treatment="roll_up",
    )

    return {
        "income_marginal": income_m,
        "age_marginal": age_m,
        "authoritative_total": auth_total,
        "raked_joint": raked_df,
        "ipf_converged": raked["converged"],
        "tail": tail,
        "decomposed_joint": decomposed,
        "lifestages": lifestages,
    }


# ---------------------------------------------------------------------------
# Synthetic PUMS — used by the always-runs test
# ---------------------------------------------------------------------------

def _synthesize_pums(seed: int = 42, n: int = 800) -> pd.DataFrame:
    """
    Generate a PUMS-shaped DataFrame loosely calibrated to a Chicago-suburb
    profile. Mix of renter/owner, broad income spread, age range 18-90.

    Realistic enough that the tail decomposition won't trigger the
    low-sample flag at min_unweighted_n=20 (the n above $150k will sit
    around 30-50 records per tenure depending on the seed).
    """
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "PUMA": rng.choice(["09701", "09702", "09703", "09704", "09705"], size=n),
        "WGTP": rng.integers(low=20, high=180, size=n).astype(float),
        "TEN":  rng.choice([1, 2, 3, 4], size=n, p=[0.45, 0.18, 0.30, 0.07]),
        "HINCP": rng.choice(
            [10_000, 20_000, 30_000, 45_000, 60_000, 85_000, 120_000,
             175_000, 225_000, 300_000, 425_000, 600_000, -3_000],
            size=n,
            p=[0.10, 0.08, 0.10, 0.12, 0.15, 0.13, 0.12, 0.08, 0.05, 0.04, 0.02, 0.01, 0.00],
        ).astype(float),
        "AGEP": rng.integers(low=18, high=90, size=n).astype(float),
        "NP":   rng.integers(low=1, high=7, size=n).astype(float),
    })


# ---------------------------------------------------------------------------
# Pick a tract that has non-zero counts on both tenure subtotals
# ---------------------------------------------------------------------------

def _pick_dense_tract(tracts: pd.DataFrame) -> pd.Series:
    """Pick a Lake County tract with the largest combined renter+owner total
    so neither marginal degenerates. Stable across runs."""
    df = tracts.dropna(subset=["B25003_002E", "B25003_003E"]).copy()
    df["_combined"] = df["B25003_002E"].astype(int) + df["B25003_003E"].astype(int)
    df = df[df["B25003_003E"].astype(int) > 100]  # need enough renters for marginals
    df = df[df["B25003_002E"].astype(int) > 100]
    df = df.sort_values("_combined", ascending=False)
    return df.iloc[0]


# ---------------------------------------------------------------------------
# Shared invariant assertions
# ---------------------------------------------------------------------------

def _assert_marginal_invariants(result: dict, tenure_label: str):
    """Marginal sums approximately equal the authoritative tenure total."""
    auth = result["authoritative_total"]
    assert auth is not None, f"{tenure_label}: authoritative_total is None"
    income_sum = sum(result["income_marginal"].values())
    age_sum = sum(result["age_marginal"].values())
    # Bracket suppression at small tracts can introduce small differences;
    # 5% tolerance is a soft sanity check rather than a strict invariant.
    assert abs(income_sum - auth) <= max(50, 0.05 * auth), (
        f"{tenure_label}: income marginal sum {income_sum} vs authoritative {auth}"
    )
    assert abs(age_sum - auth) <= max(50, 0.05 * auth), (
        f"{tenure_label}: age marginal sum {age_sum} vs authoritative {auth}"
    )


def _assert_post_rake_invariants(result: dict, tenure_label: str):
    """Post-rake crosstab column sums equal the input age marginal exactly."""
    raked = result["raked_joint"]
    col_sums = raked.groupby("age_bin")["weight"].sum()
    for age_bin, expected in result["age_marginal"].items():
        actual = int(col_sums.get(age_bin, 0))
        assert actual == int(expected), (
            f"{tenure_label}: post-rake age column {age_bin} sums to {actual}, "
            f"expected {expected}"
        )


def _assert_tail_decomp_preserves_columns(result: dict, tenure_label: str):
    """Tail decomposition preserves each age column's total exactly."""
    raked = result["raked_joint"]
    decomp = result["decomposed_joint"]
    in_cols = raked.groupby("age_bin")["weight"].sum().to_dict()
    out_cols = decomp.groupby("age_bin")["weight"].sum().to_dict()
    for age_bin, expected in in_cols.items():
        actual = int(out_cols.get(age_bin, 0))
        assert actual == int(expected), (
            f"{tenure_label}: tail-decomp age column {age_bin} drifted "
            f"{int(expected)} -> {actual}"
        )


def _assert_lifestage_total_matches_rake(result: dict, tenure_label: str):
    """Lifestage rollup total ≈ raked crosstab total (post-rake total = sum
    of marginals when row/col sums agree, which they do here by construction)."""
    raked_total = int(result["raked_joint"]["weight"].sum())
    lifestage_total = sum(result["lifestages"].values())
    assert lifestage_total == raked_total, (
        f"{tenure_label}: lifestage total {lifestage_total} vs raked total {raked_total}"
    )


def _assert_tail_shares_well_formed(result: dict, tenure_label: str):
    """Tail shares sum to 1.0 when nonzero, or 0.0 when no records above threshold."""
    s = sum(result["tail"]["shares"].values())
    if result["tail"]["weighted_total_above_threshold"] > 0:
        assert s == pytest.approx(1.0, abs=1e-9), (
            f"{tenure_label}: tail shares sum {s} != 1.0"
        )
    else:
        assert s == pytest.approx(0.0)


# ===========================================================================
# Test 1 — synthetic PUMS, always runs
# ===========================================================================

def test_e2e_synthetic_pums():
    """Compositional API correctness with a synthetic PUMS DataFrame.

    Real Lake County tract data, synthetic PUMS. Exercises every primitive
    end-to-end and verifies the marginal-sum, column-sum, and lifestage
    invariants. Does NOT validate real-data behavior — that's what
    test_e2e_live_pums is for.
    """
    if not LAKE_TRACTS.exists():
        pytest.skip("Lake County tract fixture missing.")

    tracts = pd.read_parquet(LAKE_TRACTS)
    tract = _pick_dense_tract(tracts)
    pums = _synthesize_pums(seed=42, n=800)

    # --- Renter ---
    renter = _run_pipeline_one_tract(
        tract_row=tract, pums=pums,
        tenure_filter=lambda d: d["TEN"].isin([3, 4]),
        income_config=INCOME_RENTER_CONFIG,
        age_config=AGE_RENTER_CONFIG,
        authoritative_total_var="B25003_003E",
    )
    _assert_marginal_invariants(renter, "renter")
    _assert_post_rake_invariants(renter, "renter")
    _assert_tail_shares_well_formed(renter, "renter")
    _assert_tail_decomp_preserves_columns(renter, "renter")
    _assert_lifestage_total_matches_rake(renter, "renter")

    # --- Owner ---
    owner = _run_pipeline_one_tract(
        tract_row=tract, pums=pums,
        tenure_filter=lambda d: d["TEN"].isin([1, 2]),
        income_config=INCOME_OWNER_CONFIG,
        age_config=AGE_OWNER_CONFIG,
        authoritative_total_var="B25003_002E",
    )
    _assert_marginal_invariants(owner, "owner")
    _assert_post_rake_invariants(owner, "owner")
    _assert_tail_shares_well_formed(owner, "owner")
    _assert_tail_decomp_preserves_columns(owner, "owner")
    _assert_lifestage_total_matches_rake(owner, "owner")

    # Lifestage labels must come from the configured grid; both runs see the
    # same five labels (zero-fill on labels that received nothing is allowed).
    assert set(renter["lifestages"].keys()) == set(RCLCO_DEFAULT_LIFESTAGE_GRID["label_order"])
    assert set(owner["lifestages"].keys()) == set(RCLCO_DEFAULT_LIFESTAGE_GRID["label_order"])

    # Renter and owner totals should not be wildly different from each other
    # (synthetic seed is mixed). Loose check.
    assert sum(renter["lifestages"].values()) > 0
    assert sum(owner["lifestages"].values()) > 0


# ===========================================================================
# Test 2 — live Lake County PUMS, auto-skipped until Census endpoint recovers
# ===========================================================================

@pytest.mark.skipif(
    not LAKE_PUMS.exists(),
    reason="Lake County PUMS fixture missing — Census PUMS endpoint outage. "
           "Re-run scripts/generate_lake_county_fixtures.py.",
)
def test_e2e_live_pums():
    """Compositional API on real Lake County data.

    Activates automatically when the Lake County PUMS fixture exists
    (Census endpoint outage during sessions 3 + 4 prevented capture)."""
    if not LAKE_TRACTS.exists():
        pytest.skip("Lake County tract fixture missing.")

    tracts = pd.read_parquet(LAKE_TRACTS)
    tract = _pick_dense_tract(tracts)
    pums = pd.read_parquet(LAKE_PUMS)

    renter = _run_pipeline_one_tract(
        tract_row=tract, pums=pums,
        tenure_filter=lambda d: d["TEN"].isin([3, 4]),
        income_config=INCOME_RENTER_CONFIG,
        age_config=AGE_RENTER_CONFIG,
        authoritative_total_var="B25003_003E",
    )
    _assert_marginal_invariants(renter, "renter")
    _assert_post_rake_invariants(renter, "renter")
    _assert_tail_shares_well_formed(renter, "renter")
    _assert_tail_decomp_preserves_columns(renter, "renter")
    _assert_lifestage_total_matches_rake(renter, "renter")

    owner = _run_pipeline_one_tract(
        tract_row=tract, pums=pums,
        tenure_filter=lambda d: d["TEN"].isin([1, 2]),
        income_config=INCOME_OWNER_CONFIG,
        age_config=AGE_OWNER_CONFIG,
        authoritative_total_var="B25003_002E",
    )
    _assert_marginal_invariants(owner, "owner")
    _assert_post_rake_invariants(owner, "owner")
    _assert_tail_shares_well_formed(owner, "owner")
    _assert_tail_decomp_preserves_columns(owner, "owner")
    _assert_lifestage_total_matches_rake(owner, "owner")

    # On real Lake County data, the renter $150k+ tail should clear sample
    # minimum and produce non-zero shares; the owner side likewise.
    assert renter["tail"]["weighted_total_above_threshold"] > 0
    assert owner["tail"]["weighted_total_above_threshold"] > 0
    assert renter["tail"]["low_sample_flag"] is False
    assert owner["tail"]["low_sample_flag"] is False
