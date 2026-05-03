"""
Byte-identity parity tests against Core_BTR's reference implementation.

The package's primitives must produce identical output to Core_BTR's
existing pipeline when invoked with BTR-equivalent configuration. This is
the strongest correctness check of the port: both implementations
literally produce the same numbers for the same input.

Test layout:
  - test_tract_marginals_*: every Lake County tract from the live ACS5
    fixture is run through both Core_BTR's `bin_b25{118,007,009}_renter_row`
    and the package's `compute_tract_marginal`. Output dicts must match
    cell-for-cell across all 160 tracts and all three dimensions.
  - test_subtotal_moe_flagged_matches_core_btr: same MOE flag value as
    Core_BTR's `_subtotal_moe_flagged` for every tract row, three subtotal
    cells.
  - test_ipf_parity_*: 2-D rake on a synthetic prior + Lake County tract
    marginals. Output joint dict must match Core_BTR's `ipf_2d` exactly.
  - test_puma_crosstab_parity_*: synthetic PUMS-shaped DataFrame run
    through both Core_BTR's `compute_puma_crosstabs` and the package's
    `compute_puma_joint` + `joint_to_column_major`. Crosstab dicts
    byte-equal.

Imports Core_BTR directly via sys.path. If Core_BTR is not present at
C:\\Users\\ndste\\Core_BTR, the entire module is skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


CORE_BTR_ROOT = Path(r"C:\Users\ndste\Core_BTR")
if CORE_BTR_ROOT.exists():
    sys.path.insert(0, str(CORE_BTR_ROOT))

# --- imports guarded so the test module skips cleanly if Core_BTR moves ---
try:
    from backend.config.demographic_bins import (  # type: ignore
        AGE_BIN_IDS,
        B25007_RENTER_DENOMINATOR,
        B25007_RENTER_TO_DISPLAY,
        B25009_RENTER_DENOMINATOR,
        B25009_RENTER_TO_DISPLAY,
        B25118_RENTER_DENOMINATOR,
        B25118_RENTER_TO_DISPLAY,
        HH_SIZE_BIN_IDS,
        INCOME_BIN_IDS,
        _bin_acs_row,
        bin_agep_series,
        bin_b25007_renter_row,
        bin_b25009_renter_row,
        bin_b25118_renter_row,
        bin_hincp_series,
        bin_np_series,
    )
    from backend.ipf_crosstab import ipf_2d  # type: ignore
    from backend.puma_crosstab import compute_puma_crosstabs  # type: ignore
    CORE_BTR_AVAILABLE = True
except ImportError as exc:  # pragma: no cover
    CORE_BTR_AVAILABLE = False
    _IMPORT_ERR = str(exc)

pytestmark = pytest.mark.skipif(
    not CORE_BTR_AVAILABLE,
    reason="Core_BTR not importable from C:\\Users\\ndste\\Core_BTR — parity tests skipped",
)

from telltalere_census.ipf import rake_to_marginals
from telltalere_census.puma_crosstab import (
    compute_puma_joint,
    joint_to_column_major,
)
from telltalere_census.tract_marginals import (
    BracketConfig,
    compute_tract_marginal,
    subtotal_moe_flagged,
)


FIXTURES = Path(__file__).parent / "fixtures"
LAKE_TRACTS = FIXTURES / "lake_county_tracts.parquet"
LAKE_PUMS = FIXTURES / "lake_county_pums.parquet"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def lake_tracts() -> pd.DataFrame:
    if not LAKE_TRACTS.exists():
        pytest.skip(
            f"Lake County tract fixture not present at {LAKE_TRACTS}. "
            "Run `python scripts/generate_lake_county_fixtures.py`."
        )
    return pd.read_parquet(LAKE_TRACTS)


@pytest.fixture(scope="module")
def income_config() -> BracketConfig:
    return BracketConfig(
        denominator_var=B25118_RENTER_DENOMINATOR,
        bracket_to_bin=dict(B25118_RENTER_TO_DISPLAY),
        bin_order=list(INCOME_BIN_IDS),
    )


@pytest.fixture(scope="module")
def age_config() -> BracketConfig:
    return BracketConfig(
        denominator_var=B25007_RENTER_DENOMINATOR,
        bracket_to_bin=dict(B25007_RENTER_TO_DISPLAY),
        bin_order=list(AGE_BIN_IDS),
    )


@pytest.fixture(scope="module")
def hh_size_config() -> BracketConfig:
    return BracketConfig(
        denominator_var=B25009_RENTER_DENOMINATOR,
        bracket_to_bin=dict(B25009_RENTER_TO_DISPLAY),
        bin_order=list(HH_SIZE_BIN_IDS),
    )


# ---------------------------------------------------------------------------
# tract_marginals parity
# ---------------------------------------------------------------------------

def test_tract_marginals_income_byte_identical(
    lake_tracts: pd.DataFrame, income_config: BracketConfig,
):
    diffs = []
    for _, row in lake_tracts.iterrows():
        ours = compute_tract_marginal(row, income_config)
        theirs = bin_b25118_renter_row(row)
        if ours != theirs:
            diffs.append((row["GEOID"], ours, theirs))
    assert not diffs, (
        f"Income marginal differs on {len(diffs)} tract(s); first: {diffs[0]}"
    )


def test_tract_marginals_age_byte_identical(
    lake_tracts: pd.DataFrame, age_config: BracketConfig,
):
    diffs = []
    for _, row in lake_tracts.iterrows():
        ours = compute_tract_marginal(row, age_config)
        theirs = bin_b25007_renter_row(row)
        if ours != theirs:
            diffs.append((row["GEOID"], ours, theirs))
    assert not diffs, (
        f"Age marginal differs on {len(diffs)} tract(s); first: {diffs[0]}"
    )


def test_tract_marginals_hh_size_byte_identical(
    lake_tracts: pd.DataFrame, hh_size_config: BracketConfig,
):
    diffs = []
    for _, row in lake_tracts.iterrows():
        ours = compute_tract_marginal(row, hh_size_config)
        theirs = bin_b25009_renter_row(row)
        if ours != theirs:
            diffs.append((row["GEOID"], ours, theirs))
    assert not diffs, (
        f"HH-size marginal differs on {len(diffs)} tract(s); first: {diffs[0]}"
    )


def _core_btr_subtotal_moe_flagged(row: pd.Series, est_code: str) -> bool:
    """Reimplementation of Core_BTR's _subtotal_moe_flagged for parity comparison.
    The function lives in tract_marginals.py which transitively imports bg_demand
    (heavy dep chain), so we duplicate the 8-line logic here using the canonical
    0.40 threshold from bg_demand.MOE_CV_THRESHOLD."""
    moe_col = est_code[:-1] + "M"
    est = row.get(est_code)
    moe = row.get(moe_col)
    if (
        est is None or pd.isna(est) or est == 0
        or moe is None or pd.isna(moe)
    ):
        return False
    cv = float(abs(moe)) / float(abs(est))
    return cv > 0.40


def test_subtotal_moe_flagged_matches_core_btr(lake_tracts: pd.DataFrame):
    diffs = []
    for _, row in lake_tracts.iterrows():
        for code in (
            B25118_RENTER_DENOMINATOR,
            B25007_RENTER_DENOMINATOR,
            B25009_RENTER_DENOMINATOR,
        ):
            ours = subtotal_moe_flagged(row, code)
            theirs = _core_btr_subtotal_moe_flagged(row, code)
            if ours != theirs:
                diffs.append((row["GEOID"], code, ours, theirs))
    assert not diffs, f"MOE flag differs on {len(diffs)} (tract, code); first: {diffs[0]}"


# ---------------------------------------------------------------------------
# IPF parity (synthetic prior + real Lake County marginals)
# ---------------------------------------------------------------------------

def _make_synthetic_prior() -> dict[str, dict[str, int]]:
    """A non-uniform synthetic income x age prior shaped like a typical
    PUMA's renter crosstab — most mass at low-income / young, some at
    older-renter, sparse at high income. 6 income bins x 6 age bins.
    Hand-set so no row is fully zero (avoids triggering the fallback
    rows-uniform branch in IPF, which is verified separately by the
    synthetic ipf tests)."""
    return {
        "under_25": {"under_35k": 80, "35_50k": 20, "50_75k": 10, "75_100k":  3, "100_150k":  2, "150k_plus":  1},
        "25_34":    {"under_35k": 60, "35_50k": 35, "50_75k": 40, "75_100k": 25, "100_150k": 15, "150k_plus":  8},
        "35_44":    {"under_35k": 40, "35_50k": 25, "50_75k": 35, "75_100k": 20, "100_150k": 18, "150k_plus": 10},
        "45_54":    {"under_35k": 35, "35_50k": 22, "50_75k": 28, "75_100k": 18, "100_150k": 14, "150k_plus":  8},
        "55_64":    {"under_35k": 30, "35_50k": 18, "50_75k": 22, "75_100k": 12, "100_150k":  9, "150k_plus":  5},
        "65_plus":  {"under_35k": 50, "35_50k": 18, "50_75k": 15, "75_100k":  8, "100_150k":  5, "150k_plus":  2},
    }


def test_ipf_byte_identical_synthetic_inputs():
    """Synthetic 6x6 prior + manually-constructed marginals.
    rake_to_marginals must produce exactly the same joint dict as ipf_2d."""
    prior = _make_synthetic_prior()
    row_marginal = {
        "under_35k": 600, "35_50k": 200, "50_75k": 200,
        "75_100k": 100, "100_150k": 60, "150k_plus": 40,
    }
    col_marginal = {
        "under_25": 80, "25_34": 320, "35_44": 280,
        "45_54": 240, "55_64": 180, "65_plus": 100,
    }

    ours = rake_to_marginals(prior, row_marginal, col_marginal)
    theirs = ipf_2d(prior, row_marginal, col_marginal)

    assert ours["joint"] == theirs["joint"]
    assert ours["converged"] == theirs["converged"]
    assert ours["iterations"] == theirs["iterations"]
    assert ours["rounding_residual"] == theirs["rounding_residual"]


def test_ipf_byte_identical_real_lake_county_marginals(
    lake_tracts: pd.DataFrame,
    income_config: BracketConfig,
    age_config: BracketConfig,
):
    """For each of the first 50 Lake County tracts (cap to keep test fast),
    derive the income and age marginals via the package's tract_marginals,
    then rake the synthetic prior to those marginals through both
    implementations. Both must produce byte-identical joint dicts."""
    prior = _make_synthetic_prior()
    diffs = []
    sample = lake_tracts.head(50)
    for _, row in sample.iterrows():
        income_m = compute_tract_marginal(row, income_config)
        age_m = compute_tract_marginal(row, age_config)
        ours = rake_to_marginals(prior, income_m, age_m)
        theirs = ipf_2d(prior, income_m, age_m)
        if ours["joint"] != theirs["joint"]:
            diffs.append((row["GEOID"], ours["joint"], theirs["joint"]))
    assert not diffs, (
        f"IPF joint differs on {len(diffs)} tract(s); first GEOID: {diffs[0][0]}"
    )


# ---------------------------------------------------------------------------
# puma_crosstab parity (synthetic PUMS-shaped DataFrame)
# ---------------------------------------------------------------------------

def _make_synthetic_pums() -> pd.DataFrame:
    """A small PUMS-shaped DataFrame with the columns Core_BTR's
    compute_puma_crosstabs reads: PUMA, WGTP, TEN, HINCP, AGEP, NP.
    Mix of renters (TEN ∈ {3, 4}) and owners across two PUMAs and a
    spread of incomes / ages / household sizes."""
    rng = np.random.default_rng(42)
    n = 200
    return pd.DataFrame({
        "PUMA": rng.choice(["09701", "09702"], size=n),
        "WGTP": rng.integers(low=15, high=120, size=n).astype(float),
        "TEN": rng.choice([1, 2, 3, 4], size=n, p=[0.35, 0.20, 0.30, 0.15]),
        "HINCP": rng.choice(
            [10_000, 20_000, 35_000, 45_000, 60_000, 85_000, 120_000, 200_000, -5_000],
            size=n,
        ).astype(float),
        "AGEP": rng.integers(low=18, high=85, size=n).astype(float),
        "NP": rng.integers(low=1, high=8, size=n).astype(float),
    })


def test_puma_crosstab_income_x_age_byte_identical():
    """Match Core_BTR's compute_puma_crosstabs.crosstabs.income_x_age
    against the package's compute_puma_joint -> joint_to_column_major flow."""
    pums = _make_synthetic_pums()

    # --- Core_BTR side ---
    theirs = compute_puma_crosstabs(pums, "09701")["crosstabs"]["income_x_age"]

    # --- Package side: pre-bin same way Core_BTR does internally, then
    # call compute_puma_joint with a renter filter, then convert via
    # joint_to_column_major using the canonical INCOME_BIN_IDS / AGE_BIN_IDS.
    binned = pums[pums["PUMA"].astype(str) == "09701"].copy()
    binned["_income_bin"] = bin_hincp_series(binned["HINCP"]).astype(object).where(
        bin_hincp_series(binned["HINCP"]).notna(), other=pd.NA,
    )
    binned["_age_bin"] = bin_agep_series(binned["AGEP"]).astype(object).where(
        bin_agep_series(binned["AGEP"]).notna(), other=pd.NA,
    )
    joint = compute_puma_joint(
        binned,
        dimensions=["_income_bin", "_age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    ours = joint_to_column_major(
        joint,
        row_dim="_income_bin", col_dim="_age_bin",
        row_order=list(INCOME_BIN_IDS), col_order=list(AGE_BIN_IDS),
    )

    assert ours == theirs


def test_puma_crosstab_income_x_hh_size_byte_identical():
    pums = _make_synthetic_pums()
    theirs = compute_puma_crosstabs(pums, "09702")["crosstabs"]["income_x_hh_size"]

    binned = pums[pums["PUMA"].astype(str) == "09702"].copy()
    binned["_income_bin"] = bin_hincp_series(binned["HINCP"]).astype(object).where(
        bin_hincp_series(binned["HINCP"]).notna(), other=pd.NA,
    )
    binned["_hh_size_bin"] = bin_np_series(binned["NP"]).astype(object).where(
        bin_np_series(binned["NP"]).notna(), other=pd.NA,
    )
    joint = compute_puma_joint(
        binned,
        dimensions=["_income_bin", "_hh_size_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    ours = joint_to_column_major(
        joint,
        row_dim="_income_bin", col_dim="_hh_size_bin",
        row_order=list(INCOME_BIN_IDS), col_order=list(HH_SIZE_BIN_IDS),
    )
    assert ours == theirs


def test_puma_crosstab_1d_marginals_byte_identical():
    """Core_BTR's compute_puma_crosstabs.marginals.income / .age / .hh_size
    each match the package's compute_puma_joint with a single dimension."""
    pums = _make_synthetic_pums()
    btr = compute_puma_crosstabs(pums, "09701")["marginals"]

    sub = pums[pums["PUMA"].astype(str) == "09701"].copy()
    sub["_income_bin"] = bin_hincp_series(sub["HINCP"]).astype(object).where(
        bin_hincp_series(sub["HINCP"]).notna(), other=pd.NA,
    )
    sub["_age_bin"] = bin_agep_series(sub["AGEP"]).astype(object).where(
        bin_agep_series(sub["AGEP"]).notna(), other=pd.NA,
    )
    sub["_hh_size_bin"] = bin_np_series(sub["NP"]).astype(object).where(
        bin_np_series(sub["NP"]).notna(), other=pd.NA,
    )
    renter_filter = lambda d: d["TEN"].isin([3, 4])  # noqa: E731

    inc = compute_puma_joint(sub, ["_income_bin"], filter_func=renter_filter)
    age = compute_puma_joint(sub, ["_age_bin"], filter_func=renter_filter)
    hhs = compute_puma_joint(sub, ["_hh_size_bin"], filter_func=renter_filter)

    inc_dict = {bid: int(inc.loc[bid, "weight"]) if bid in inc.index else 0
                for bid in INCOME_BIN_IDS}
    age_dict = {bid: int(age.loc[bid, "weight"]) if bid in age.index else 0
                for bid in AGE_BIN_IDS}
    hhs_dict = {bid: int(hhs.loc[bid, "weight"]) if bid in hhs.index else 0
                for bid in HH_SIZE_BIN_IDS}

    assert inc_dict == btr["income"]
    assert age_dict == btr["age"]
    assert hhs_dict == btr["hh_size"]


# ---------------------------------------------------------------------------
# Live PUMS parity (skipped until Census PUMS endpoint recovers and the
# fixture script captures lake_county_pums.parquet)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not LAKE_PUMS.exists(),
    reason="Live PUMS fixture missing — Census PUMS endpoint was 500ing at "
           "fixture-generation time. Re-run scripts/generate_lake_county_fixtures.py.",
)
def test_puma_crosstab_byte_identical_live_pums():
    """Real Lake County PUMS pulled from Census API; same byte-identity
    check as the synthetic version. Activated automatically when the live
    fixture is present."""
    pums = pd.read_parquet(LAKE_PUMS)
    puma_code = pums["PUMA"].astype(str).iloc[0]

    theirs = compute_puma_crosstabs(pums, puma_code)["crosstabs"]["income_x_age"]

    sub = pums[pums["PUMA"].astype(str) == puma_code].copy()
    sub["_income_bin"] = bin_hincp_series(sub["HINCP"]).astype(object).where(
        bin_hincp_series(sub["HINCP"]).notna(), other=pd.NA,
    )
    sub["_age_bin"] = bin_agep_series(sub["AGEP"]).astype(object).where(
        bin_agep_series(sub["AGEP"]).notna(), other=pd.NA,
    )
    joint = compute_puma_joint(
        sub,
        dimensions=["_income_bin", "_age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    ours = joint_to_column_major(
        joint,
        row_dim="_income_bin", col_dim="_age_bin",
        row_order=list(INCOME_BIN_IDS), col_order=list(AGE_BIN_IDS),
    )
    assert ours == theirs
