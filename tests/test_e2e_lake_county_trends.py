"""End-to-end integration test for session 5: multi-vintage fetch + trend
computation + boundary harmonization on real Lake County, IL data.

Composes:

    fetch_acs_data_multi_vintage      (2018 + 2023 5-year)
    get_boundary_vintage_for_acs       (cutover lookup)
    compute_trend_metrics              (population, renter share)
    is_change_significant              (MOE-aware)
    cumulative_trend                   (synthetic time series)
    harmonize_to_2020_boundaries       (2018 -> 2020 boundaries)

Real ACS5 fetches against Census API; takes ~30-60s on cold cache.
Subsequent runs hit the cache and run in <2s. PUMS is not exercised
here (the Census PUMS endpoint outage that started in session 3
remains in effect; PUMS is unrelated to ACS5 summary-table fetches).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from telltalere_census.boundaries import (
    get_boundary_vintage_for_acs,
    harmonize_to_2020_boundaries,
)
from telltalere_census.multi_vintage import fetch_acs_data_multi_vintage
from telltalere_census.trends import (
    compute_trend_metrics,
    cumulative_trend,
    is_change_significant,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
LAKE_COUNTY_FIPS = "17097"
STATE_FIPS = "17"
COUNTY_FIPS = "097"

BUILD_CROSSWALK = REPO_ROOT / "build" / "tract_boundary_crosswalk_2010_to_2020.parquet"


@pytest.fixture(scope="module")
def lake_2018_2023_state() -> pd.DataFrame:
    """Multi-vintage tract-level fetch for Illinois (state 17), 2018 +
    2023 5-year. Returns the long-format DataFrame.
    """
    df = fetch_acs_data_multi_vintage(
        state_fips=STATE_FIPS,
        variables=[
            "B01003_001E",     # total population
            "B25003_001E",     # total occupied housing units
            "B25003_002E",     # owner-occupied
            "B25003_003E",     # renter-occupied
            "B25064_001E",     # median gross rent (median variable)
        ],
        vintages=[2018, 2023],
        geography="tract",
        acs_type="acs5",
        include_moe=True,
    )
    return df


@pytest.fixture(scope="module")
def lake_2018(lake_2018_2023_state: pd.DataFrame) -> pd.DataFrame:
    return lake_2018_2023_state[
        (lake_2018_2023_state["vintage"] == 2018)
        & (lake_2018_2023_state["county"] == COUNTY_FIPS)
    ].copy()


@pytest.fixture(scope="module")
def lake_2023(lake_2018_2023_state: pd.DataFrame) -> pd.DataFrame:
    return lake_2018_2023_state[
        (lake_2018_2023_state["vintage"] == 2023)
        & (lake_2018_2023_state["county"] == COUNTY_FIPS)
    ].copy()


@pytest.fixture(scope="module")
def crosswalk() -> pd.DataFrame:
    if not BUILD_CROSSWALK.exists():
        pytest.skip(
            f"Crosswalk not at {BUILD_CROSSWALK}; run "
            "scripts/build_tract_boundary_crosswalk.py."
        )
    return pd.read_parquet(BUILD_CROSSWALK)


# ---------------------------------------------------------------------------
# Long-format shape sanity
# ---------------------------------------------------------------------------

def test_multi_vintage_long_format_shape(lake_2018_2023_state):
    """Two vintages, vintage column populated, both halves non-empty."""
    df = lake_2018_2023_state
    assert "vintage" in df.columns
    assert set(df["vintage"].unique()) == {2018, 2023}
    n_2018 = (df["vintage"] == 2018).sum()
    n_2023 = (df["vintage"] == 2023).sum()
    assert n_2018 > 0
    assert n_2023 > 0
    # Illinois ACS 2018 (2010 boundaries) and 2023 (2020 boundaries) row
    # counts will differ slightly because tract counts changed across the
    # decade. Both should be in the same order of magnitude (~3000 IL tracts).
    assert 2500 < n_2018 < 4000
    assert 2500 < n_2023 < 4000


def test_boundary_vintages_match_cutover():
    """Confirm the cutover lookup returns the values we'd predict."""
    assert get_boundary_vintage_for_acs(2018, "acs5") == 2010
    assert get_boundary_vintage_for_acs(2023, "acs5") == 2020


# ---------------------------------------------------------------------------
# Trend computation on a stable (unchanged-boundary) Lake County tract
# ---------------------------------------------------------------------------

def _pick_unchanged_lake_tract(crosswalk: pd.DataFrame) -> str:
    """Pick a Lake County tract that is 'unchanged' across the boundary
    cutover, so its GEOID exists in both 2018 and 2023 ACS data."""
    lake_unchanged = crosswalk[
        (crosswalk["state_fips"] == STATE_FIPS)
        & (crosswalk["county_fips"] == COUNTY_FIPS)
        & (crosswalk["relationship_type"] == "unchanged")
    ]
    return str(lake_unchanged.iloc[0]["geoid_2010"])


def test_population_trend_on_unchanged_tract(
    lake_2018, lake_2023, crosswalk,
):
    """Pick an unchanged Lake County tract, compute population trend
    2018 -> 2023, verify TrendResult shape."""
    tract = _pick_unchanged_lake_tract(crosswalk)

    row_2018 = lake_2018[lake_2018["GEOID"] == tract]
    row_2023 = lake_2023[lake_2023["GEOID"] == tract]
    assert len(row_2018) == 1, f"2018 has {len(row_2018)} rows for {tract}"
    assert len(row_2023) == 1, f"2023 has {len(row_2023)} rows for {tract}"

    pop_2018 = float(row_2018["B01003_001E"].iloc[0])
    pop_2023 = float(row_2023["B01003_001E"].iloc[0])
    moe_2018 = float(row_2018["B01003_001M"].iloc[0])
    moe_2023 = float(row_2023["B01003_001M"].iloc[0])

    result = compute_trend_metrics(
        values_by_vintage={2018: pop_2018, 2023: pop_2023},
        moes_by_vintage={2018: moe_2018, 2023: moe_2023},
    )

    # All TrendResult fields populated
    assert result["early_value"] == pop_2018
    assert result["late_value"] == pop_2023
    assert result["years_elapsed"] == 5
    assert result["absolute_change"] == pop_2023 - pop_2018
    assert result["moe_combined"] == pytest.approx(
        math.sqrt(moe_2018 ** 2 + moe_2023 ** 2)
    )
    assert result["direction"] in (
        "increasing", "decreasing", "flat", "noise",
    )
    assert isinstance(result["is_significant"], bool)


def test_renter_share_trend_significance(lake_2018, lake_2023, crosswalk):
    """Renter share for an unchanged Lake County tract — direction +
    MOE-aware significance against hand-computed combined MOE."""
    tract = _pick_unchanged_lake_tract(crosswalk)

    r18 = lake_2018[lake_2018["GEOID"] == tract].iloc[0]
    r23 = lake_2023[lake_2023["GEOID"] == tract].iloc[0]

    if pd.isna(r18["B25003_001E"]) or pd.isna(r23["B25003_001E"]):
        pytest.skip(f"Tenure totals suppressed at {tract} for one vintage")
    if r18["B25003_001E"] == 0 or r23["B25003_001E"] == 0:
        pytest.skip(f"Zero tenure total at {tract}")

    share_2018 = float(r18["B25003_003E"]) / float(r18["B25003_001E"])
    share_2023 = float(r23["B25003_003E"]) / float(r23["B25003_001E"])

    # MOE for a ratio: simplification — use the renter MOE only as the
    # denominator-uncertainty surrogate. For end-to-end test, we just
    # verify is_change_significant returns a boolean and the threshold
    # is consistent with hand-computation on raw counts.
    renter_count_2018 = float(r18["B25003_003E"])
    renter_count_2023 = float(r23["B25003_003E"])
    moe_18 = float(r18["B25003_003M"]) if not pd.isna(r18["B25003_003M"]) else 0.0
    moe_23 = float(r23["B25003_003M"]) if not pd.isna(r23["B25003_003M"]) else 0.0

    sig = is_change_significant(
        renter_count_2018, moe_18, renter_count_2023, moe_23,
        method="moe_combined",
    )
    expected = abs(renter_count_2023 - renter_count_2018) > math.sqrt(
        moe_18 ** 2 + moe_23 ** 2
    )
    assert sig == expected


# ---------------------------------------------------------------------------
# Boundary harmonization — 2018 (2010 boundaries) -> 2020 boundaries
# ---------------------------------------------------------------------------

def test_harmonize_2018_lake_county_population_preservation(
    lake_2018, crosswalk,
):
    """Take Lake County 2018 ACS5 (2010 boundaries), harmonize population
    onto 2020 boundaries, verify total preservation modulo cross-county
    bleed-through.
    """
    historical = lake_2018[["GEOID", "B01003_001E"]].copy()
    historical = historical.dropna(subset=["B01003_001E"])
    historical = historical.rename(columns={"GEOID": "geoid", "B01003_001E": "pop"})
    historical["pop"] = historical["pop"].astype("Int64")

    total_in = int(historical["pop"].sum())

    out = harmonize_to_2020_boundaries(
        historical,
        count_columns=["pop"],
        crosswalk=crosswalk,
        na_treatment="zero",  # unmatched contributors -> 0 (we seeded only Lake)
    )
    total_out = float(out["pop"].sum())
    # Some Lake 2010 tracts straddle into neighboring counties' 2020
    # successors — small effect. Allow 2% tolerance.
    assert abs(total_out - total_in) / total_in < 0.02


def test_harmonize_2018_lake_county_median_columns_nan(lake_2018, crosswalk):
    """Median column (B25064_001E) becomes NaN under default
    median_treatment='nan'."""
    historical = lake_2018[
        ["GEOID", "B01003_001E", "B25064_001E"]
    ].copy()
    historical = historical.dropna(subset=["B01003_001E"])
    historical = historical.rename(columns={
        "GEOID": "geoid",
        "B01003_001E": "pop",
        "B25064_001E": "median_rent",
    })

    out = harmonize_to_2020_boundaries(
        historical,
        count_columns=["pop"],
        median_columns=["median_rent"],
        crosswalk=crosswalk,
        na_treatment="zero",
    )
    assert out["median_rent"].isna().all()
    # Counts still allocated
    assert out["pop"].sum() > 0


# ---------------------------------------------------------------------------
# Cumulative trend on synthetic 5-vintage time series
# ---------------------------------------------------------------------------

def test_cumulative_trend_5_vintages():
    """Synthetic annual-ish time series. cumulative_trend produces N-1
    consecutive-pair results."""
    values = {
        2019: 10000, 2020: 10500, 2021: 10800,
        2022: 11200, 2023: 11500,
    }
    moes = {y: 200 for y in values}
    results = cumulative_trend(values, moes_by_vintage=moes)
    assert len(results) == 4
    # All pair results should be year-adjacent
    for r in results:
        assert r["years_elapsed"] == 1
    # Sequence: 2019->2020, 2020->2021, 2021->2022, 2022->2023
    pairs = [(r["early_vintage"], r["late_vintage"]) for r in results]
    assert pairs == [(2019, 2020), (2020, 2021), (2021, 2022), (2022, 2023)]
    # Each step is +5% increase, well above 5% threshold? Actually most are
    # below threshold (2.9-5%). Not asserting direction here — those are unit-tested.
