"""
Integration test: fetch ACS5 2024 data for Illinois (state 17) and validate
Lake County (county 097) results at both block-group and tract geography.

Lake County 2024 ACS5 authoritative values (pulled from Census county-level
summary endpoint on 2026-04-19 — tolerance is 5% against these anchors):
  B25003_001E (total occupied housing units):  258,455
  B25003_002E (owner-occupied):                193,403
  B25003_003E (renter-occupied):                65,052
  Renter share:                                25.17%

The test uses a temp cache dir so it's hermetic. First run hits the Census
API; subsequent runs within the same pytest session reuse the parquet. Mark
as `integration` so fast unit loops can skip.

Run:
    pytest tests/test_acs_fetch_lake_county.py -v -m integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from telltalere_census.acs_fetch import fetch_acs_data
from telltalere_census.variables import ACS_BG_DEFAULT_VARS


pytestmark = pytest.mark.integration


STATE_FIPS = "17"
LAKE_COUNTY_FIPS = "097"
LAKE_COUNTY_PREFIX = "17097"

# Authoritative Lake County totals from county-level ACS5 2024 call
# (pulled 2026-04-19): see module docstring. Tolerance 5%.
LAKE_TOTAL_HH = 258_455
LAKE_RENTER_HH = 65_052
LAKE_OWNER_HH = 193_403


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    """One temp cache dir shared by all tests in this module."""
    return tmp_path_factory.mktemp("acs_fetch_cache")


@pytest.fixture(scope="module")
def _api_key_note():
    """Warn if CENSUS_API_KEY is missing — not fatal, but Census throttles."""
    if not os.environ.get("CENSUS_API_KEY"):
        pytest.skip(
            "CENSUS_API_KEY not set and cache unavailable; "
            "set CENSUS_API_KEY env var or pre-populate the cache to run.",
            allow_module_level=False,
        ) if False else None  # informational only; Census allows anon queries


@pytest.fixture(scope="module")
def bg_df(cache_dir):
    """Block-group-level Illinois fetch, shared across bg tests."""
    return fetch_acs_data(
        state_fips=STATE_FIPS,
        variables=ACS_BG_DEFAULT_VARS,
        geography="block group",
        vintage=2024,
        acs_type="acs5",
        include_moe=True,
        cache_dir=cache_dir,
    )


@pytest.fixture(scope="module")
def tract_df(cache_dir):
    """Tract-level Illinois fetch, shared across tract tests."""
    return fetch_acs_data(
        state_fips=STATE_FIPS,
        variables=ACS_BG_DEFAULT_VARS,
        geography="tract",
        vintage=2024,
        acs_type="acs5",
        include_moe=True,
        cache_dir=cache_dir,
    )


# ---------------------------------------------------------------------------
# Block-group assertions
# ---------------------------------------------------------------------------

def test_bg_nonempty_lake_county(bg_df):
    lake = bg_df[bg_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    # Lake County IL has ~400+ block groups
    assert len(lake) >= 300, (
        f"expected >=300 Lake County block groups, got {len(lake)}"
    )


def test_bg_all_geoids_start_with_county_prefix(bg_df):
    lake = bg_df[bg_df["county"].astype(str).str.zfill(3) == LAKE_COUNTY_FIPS]
    assert len(lake) > 0
    assert lake["GEOID"].str.startswith(LAKE_COUNTY_PREFIX).all(), (
        "some Lake County rows have GEOIDs not starting with "
        f"{LAKE_COUNTY_PREFIX!r}"
    )
    # GEOID length: state(2) + county(3) + tract(6) + block group(1) = 12
    assert (lake["GEOID"].str.len() == 12).all()


def test_bg_total_households_within_5pct(bg_df):
    lake = bg_df[bg_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    total = lake["B25003_001E"].sum()
    pct_err = abs(float(total) - LAKE_TOTAL_HH) / LAKE_TOTAL_HH
    assert pct_err <= 0.05, (
        f"Lake County BG sum of B25003_001E = {total:,.0f} "
        f"vs authoritative {LAKE_TOTAL_HH:,} "
        f"(error {pct_err:.2%})"
    )


def test_bg_renter_share_sensible(bg_df):
    lake = bg_df[bg_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    total = float(lake["B25003_001E"].sum())
    renters = float(lake["B25003_003E"].sum())
    share = renters / total
    assert 0.20 <= share <= 0.40, (
        f"Lake County renter share {share:.2%} outside 20-40% "
        f"(primarily owner-occupied suburban county)"
    )


def test_bg_moe_columns_present(bg_df):
    est_cols = [c for c in bg_df.columns if c.endswith("E") and c.startswith("B")]
    missing = [c for c in est_cols if (c[:-1] + "M") not in bg_df.columns]
    assert not missing, (
        f"MOE companions missing for: {missing[:10]}"
        + (f" (+{len(missing)-10} more)" if len(missing) > 10 else "")
    )


def test_bg_median_rent_coverage(bg_df):
    # Census suppresses B25064_001E with sentinel -666666666 ("median cannot
    # be computed") in BGs with too few renter-occupied units. Empirical
    # coverage for Lake County 2024 ACS5 at BG level is ~60%; threshold set
    # at 50% to catch regressions without flagging normal suppression.
    lake = bg_df[bg_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    non_null_frac = lake["B25064_001E"].notna().mean()
    assert non_null_frac >= 0.50, (
        f"Median gross rent (B25064_001E) non-null rate {non_null_frac:.2%} "
        f"below 50% floor for Lake County block groups"
    )


# ---------------------------------------------------------------------------
# Tract assertions
# ---------------------------------------------------------------------------

def test_tract_nonempty_lake_county(tract_df):
    lake = tract_df[tract_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    # Lake County IL has ~110 tracts
    assert len(lake) >= 80, (
        f"expected >=80 Lake County tracts, got {len(lake)}"
    )


def test_tract_all_geoids_start_with_county_prefix(tract_df):
    lake = tract_df[tract_df["county"].astype(str).str.zfill(3) == LAKE_COUNTY_FIPS]
    assert len(lake) > 0
    assert lake["GEOID"].str.startswith(LAKE_COUNTY_PREFIX).all(), (
        "some Lake County tract rows have GEOIDs not starting with "
        f"{LAKE_COUNTY_PREFIX!r}"
    )
    # GEOID length: state(2) + county(3) + tract(6) = 11
    assert (lake["GEOID"].str.len() == 11).all()
    # Tract-level frame should NOT have block_group column
    assert "block_group" not in tract_df.columns


def test_tract_total_households_within_5pct(tract_df):
    lake = tract_df[tract_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    total = lake["B25003_001E"].sum()
    pct_err = abs(float(total) - LAKE_TOTAL_HH) / LAKE_TOTAL_HH
    assert pct_err <= 0.05, (
        f"Lake County tract sum of B25003_001E = {total:,.0f} "
        f"vs authoritative {LAKE_TOTAL_HH:,} "
        f"(error {pct_err:.2%})"
    )


def test_tract_renter_share_sensible(tract_df):
    lake = tract_df[tract_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    total = float(lake["B25003_001E"].sum())
    renters = float(lake["B25003_003E"].sum())
    share = renters / total
    assert 0.20 <= share <= 0.40, (
        f"Lake County tract renter share {share:.2%} outside 20-40%"
    )


def test_tract_moe_columns_present(tract_df):
    est_cols = [c for c in tract_df.columns if c.endswith("E") and c.startswith("B")]
    missing = [c for c in est_cols if (c[:-1] + "M") not in tract_df.columns]
    assert not missing, f"tract MOE companions missing for: {missing[:10]}"


def test_tract_median_rent_coverage_higher_than_bg(bg_df, tract_df):
    """Tract aggregation should have >= BG median-rent coverage (less suppression)."""
    lake_bg = bg_df[bg_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    lake_tract = tract_df[tract_df["GEOID"].str.startswith(LAKE_COUNTY_PREFIX)]
    bg_coverage = lake_bg["B25064_001E"].notna().mean()
    tract_coverage = lake_tract["B25064_001E"].notna().mean()
    assert tract_coverage >= bg_coverage - 0.01, (
        f"tract median-rent coverage {tract_coverage:.2%} unexpectedly lower "
        f"than BG coverage {bg_coverage:.2%}"
    )
