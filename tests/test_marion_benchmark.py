"""
Regression test: Marion County, IN (FIPS 18097), ACS5 2024.

Runs the shared pipeline end-to-end and checks the resulting weighted
distributions against a frozen baseline captured from the pre-refactor
ami-tool code path. Exact equality is required on every metric.

The baseline fixture at `tests/marion_baseline.json` was captured ONCE from
the current ami-tool pipeline before any extraction began. If it ever needs
re-capture, run `capture_marion_baseline.py` from the ami-tool repo with no
fixture present — and review the diff carefully before committing.

This test does not require HUD env vars because it exercises only the
shared Census data layer (no AMI threshold computation, no HUD calls).
The pipeline here:
  resolve_county -> get_pumas_for_county -> fetch_pums_data -> derived_fields.add_all
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from telltalere_census.county import resolve_county
from telltalere_census.puma_crosswalk import get_pumas_for_county
from telltalere_census.pums_fetch import fetch_pums_data
from telltalere_census.derived_fields import add_all


FIXTURE_PATH = Path(__file__).parent / "marion_baseline.json"
FIPS = "18097"
ACS_YEAR = 2024

# The pre-refactor ami-tool caches its parquets in ami-tool/data/.
# Pin the shared fetcher to that directory so the test reuses the cache
# rather than re-hitting the Census API.
AMI_TOOL_DATA_DIR = Path(__file__).parent.parent.parent / "ami-tool" / "data"


@pytest.fixture(scope="module")
def marion_df() -> pd.DataFrame:
    cache_dir = AMI_TOOL_DATA_DIR if AMI_TOOL_DATA_DIR.exists() else None
    state, county, _, _ = resolve_county(fips=FIPS, acs_year=ACS_YEAR)
    pumas = get_pumas_for_county(state, county, cache_dir=cache_dir)
    df = fetch_pums_data(
        state_fips=state,
        puma_codes=pumas,
        acs_type="acs5",
        acs_year=ACS_YEAR,
        cache_dir=cache_dir,
    )
    df = add_all(df)
    return df


@pytest.fixture(scope="module")
def baseline() -> dict:
    assert FIXTURE_PATH.exists(), (
        f"Baseline fixture missing at {FIXTURE_PATH}. "
        "Run ami-tool/capture_marion_baseline.py to regenerate."
    )
    return json.loads(FIXTURE_PATH.read_text())


def _weighted_by_column(df: pd.DataFrame, col: str) -> dict[str, float]:
    g = df.groupby(df[col].fillna("__na__"))["WGTP"].sum().sort_index()
    return {str(k): float(v) for k, v in g.items()}


def test_unweighted_row_count(marion_df: pd.DataFrame, baseline: dict):
    assert len(marion_df) == baseline["unweighted_rows"]


def test_weighted_households_total(marion_df: pd.DataFrame, baseline: dict):
    assert float(marion_df["WGTP"].sum()) == pytest.approx(
        baseline["weighted_households_total"], rel=0, abs=0
    )


def test_weighted_renters(marion_df: pd.DataFrame, baseline: dict):
    renters = marion_df.loc[marion_df["TEN"].isin([3, 4]), "WGTP"].sum()
    assert float(renters) == pytest.approx(baseline["weighted_renters"], rel=0, abs=0)


def test_weighted_owners(marion_df: pd.DataFrame, baseline: dict):
    owners = marion_df.loc[marion_df["TEN"].isin([1, 2]), "WGTP"].sum()
    assert float(owners) == pytest.approx(baseline["weighted_owners"], rel=0, abs=0)


def test_weighted_by_puma(marion_df: pd.DataFrame, baseline: dict):
    puma_col = "PUMA" if "PUMA" in marion_df.columns else "PUMA20"
    by_puma = (marion_df.groupby(marion_df[puma_col].astype(str).str.zfill(5))["WGTP"]
                 .sum().sort_index())
    got = {k: float(v) for k, v in by_puma.items()}
    assert got == baseline["weighted_households_by_puma"]


def test_cost_burden_distribution(marion_df: pd.DataFrame, baseline: dict):
    got = _weighted_by_column(marion_df, "cost_burden")
    assert got == baseline["cost_burden_weighted"]


def test_age_cohort_distribution(marion_df: pd.DataFrame, baseline: dict):
    got = _weighted_by_column(marion_df, "age_cohort")
    assert got == baseline["age_cohort_weighted"]


def test_building_era_distribution(marion_df: pd.DataFrame, baseline: dict):
    got = _weighted_by_column(marion_df, "building_era")
    assert got == baseline["building_era_weighted"]
