"""Unit tests for telltalere_census.boundaries."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from telltalere_census.boundaries import (
    download_tract_boundary_crosswalk,
    get_boundary_vintage_for_acs,
    harmonize_to_2020_boundaries,
    load_tract_boundary_crosswalk,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_PARQUET = REPO_ROOT / "build" / "tract_boundary_crosswalk_2010_to_2020.parquet"


@pytest.fixture(scope="module")
def real_crosswalk() -> pd.DataFrame:
    """Real built parquet from `scripts/build_tract_boundary_crosswalk.py`.
    Tests that exercise actual harmonization use this; the synthetic-only
    tests build their own minimal crosswalk inline."""
    if not BUILD_PARQUET.exists():
        pytest.skip(
            f"Built crosswalk not present at {BUILD_PARQUET}. Run "
            "`python scripts/build_tract_boundary_crosswalk.py`."
        )
    return pd.read_parquet(BUILD_PARQUET)


# ---------------------------------------------------------------------------
# get_boundary_vintage_for_acs
# ---------------------------------------------------------------------------

def test_acs5_2019_uses_2010_boundaries():
    assert get_boundary_vintage_for_acs(2019, "acs5") == 2010


def test_acs5_2020_uses_2020_boundaries():
    """The 2016-2020 5-year was the first to use 2020 Census boundaries."""
    assert get_boundary_vintage_for_acs(2020, "acs5") == 2020


def test_acs5_2024_uses_2020_boundaries():
    assert get_boundary_vintage_for_acs(2024, "acs5") == 2020


def test_acs5_2018_uses_2010_boundaries():
    assert get_boundary_vintage_for_acs(2018, "acs5") == 2010


def test_acs1_2019_uses_2010_boundaries():
    assert get_boundary_vintage_for_acs(2019, "acs1") == 2010


def test_acs1_2021_uses_2020_boundaries():
    """2020 1-year was not published; 2021 is the first 1-year on 2020 boundaries."""
    assert get_boundary_vintage_for_acs(2021, "acs1") == 2020


def test_acs1_2020_raises_not_published():
    with pytest.raises(ValueError, match="not published.*COVID"):
        get_boundary_vintage_for_acs(2020, "acs1")


def test_pre_2009_raises():
    with pytest.raises(ValueError, match="predates"):
        get_boundary_vintage_for_acs(2008, "acs5")


def test_invalid_acs_type_raises():
    with pytest.raises(ValueError, match="acs_type"):
        get_boundary_vintage_for_acs(2020, "acs3")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_tract_boundary_crosswalk
# ---------------------------------------------------------------------------

def test_load_crosswalk_raises_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BOUNDARY_CACHE", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="download_tract_boundary_crosswalk"):
        load_tract_boundary_crosswalk()


def test_load_crosswalk_invalid_direction(tmp_path, monkeypatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BOUNDARY_CACHE", str(tmp_path))
    with pytest.raises(ValueError, match="direction must be"):
        load_tract_boundary_crosswalk(direction="bogus")  # type: ignore[arg-type]


def test_load_crosswalk_via_cache_dir(real_crosswalk, tmp_path):
    """Place the built parquet in a fresh cache dir; load should succeed."""
    target = tmp_path / "tract_boundary_crosswalk_2010_to_2020.parquet"
    real_crosswalk.to_parquet(target)
    df = load_tract_boundary_crosswalk(cache_dir=tmp_path)
    assert len(df) == len(real_crosswalk)
    assert set(df.columns) == set(real_crosswalk.columns)


def test_loaded_crosswalk_schema(real_crosswalk):
    """Schema check on the as-built parquet."""
    expected_cols = {
        "geoid_2020", "geoid_2010", "state_fips", "county_fips",
        "arealand_part_sqm", "arealand_2020_sqm", "arealand_2010_sqm",
        "area_weight_2010", "area_weight_2020", "relationship_type",
    }
    assert set(real_crosswalk.columns) == expected_cols


def test_relationship_types_complete(real_crosswalk):
    """All four relationship_type categories are present in the national data."""
    types = set(real_crosswalk["relationship_type"].unique())
    assert types == {"unchanged", "split", "merge", "partial"}


def test_loaded_weights_sum_to_one_per_2010_tract_for_populated_tracts(real_crosswalk):
    """For 2010 tracts with >0 land area, area_weight_2010 sums to ~1.0
    across the rows for that tract."""
    populated = real_crosswalk[real_crosswalk["arealand_2010_sqm"] > 0]
    sums = populated.groupby("geoid_2010", observed=True)["area_weight_2010"].sum()
    drift = (sums - 1.0).abs()
    # Allow small float noise for tracts at the edge of representation
    assert (drift < 1e-6).mean() > 0.99


# ---------------------------------------------------------------------------
# harmonize_to_2020_boundaries — synthetic crosswalk
# ---------------------------------------------------------------------------

_T_A = "17097000001"
_T_B = "17097000002"
_T_B1 = "17097000003"
_T_B2 = "17097000004"
_T_M = "17097000005"
_T_M1 = "17097000006"
_T_M2 = "17097000007"


def _synthetic_crosswalk() -> pd.DataFrame:
    """A 5-row crosswalk covering three scenarios with realistic
    11-char numeric geoids (matches the production zfill convention):
      _T_A: unchanged (1:1)
      _T_B: split 50/50 into _T_B1, _T_B2
      _T_M: merge of _T_M1+_T_M2 into one _T_M
    """
    return pd.DataFrame({
        "geoid_2010":     [_T_A,  _T_B,  _T_B,  _T_M1, _T_M2],
        "geoid_2020":     [_T_A,  _T_B1, _T_B2, _T_M,  _T_M],
        "state_fips":     ["17"] * 5,
        "county_fips":    ["097"] * 5,
        "arealand_part_sqm":   [100, 50, 50, 100, 100],
        "arealand_2020_sqm":   [100, 50, 50, 200, 200],
        "arealand_2010_sqm":   [100, 100, 100, 100, 100],
        "area_weight_2010":    [1.0, 0.5, 0.5, 1.0, 1.0],
        "area_weight_2020":    [1.0, 1.0, 1.0, 0.5, 0.5],
        "relationship_type":   ["unchanged", "split", "split", "merge", "merge"],
    })


def test_harmonize_unchanged_passes_through():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_A], "pop": [1000]})
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
    )
    row = out[out["geoid_2020"] == _T_A].iloc[0]
    assert row["pop"] == 1000


def test_harmonize_split_distributes_by_weight():
    """B(1000) splits 50/50 -> B1=500, B2=500."""
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_B], "pop": [1000]})
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
    )
    assert int(out[out["geoid_2020"] == _T_B1]["pop"].iloc[0]) == 500
    assert int(out[out["geoid_2020"] == _T_B2]["pop"].iloc[0]) == 500


def test_harmonize_merge_sums_contributors():
    """M1(300) + M2(700) -> M=1000."""
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_M1, _T_M2], "pop": [300, 700]})
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
    )
    assert int(out[out["geoid_2020"] == _T_M]["pop"].iloc[0]) == 1000


def test_harmonize_population_sum_preserved_across_split():
    """Splitting a tract preserves total population."""
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_B], "pop": [1000]})
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
    )
    # Sum across the two B-children should equal 1000 (other 2020 tracts NA)
    sub = out[out["geoid_2020"].isin([_T_B1, _T_B2])]
    assert int(sub["pop"].sum()) == 1000


def test_harmonize_multiple_count_columns():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({
        "geoid": [_T_B], "pop": [1000], "households": [400]
    })
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop", "households"], crosswalk=cw,
    )
    assert int(out[out["geoid_2020"] == _T_B1]["pop"].iloc[0]) == 500
    assert int(out[out["geoid_2020"] == _T_B1]["households"].iloc[0]) == 200


def test_harmonize_median_columns_default_nan():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({
        "geoid": [_T_B], "pop": [1000], "median_rent": [1200],
    })
    out = harmonize_to_2020_boundaries(
        historical,
        count_columns=["pop"],
        median_columns=["median_rent"],
        median_treatment="nan",
        crosswalk=cw,
    )
    # All median rows are NaN
    assert out["median_rent"].isna().all()
    # Counts still allocated correctly
    assert int(out[out["geoid_2020"] == _T_B1]["pop"].iloc[0]) == 500


def test_harmonize_median_columns_raise():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({
        "geoid": [_T_B], "pop": [1000], "median_rent": [1200],
    })
    with pytest.raises(ValueError, match="median_treatment='raise'"):
        harmonize_to_2020_boundaries(
            historical,
            count_columns=["pop"],
            median_columns=["median_rent"],
            median_treatment="raise",
            crosswalk=cw,
        )


def test_harmonize_invalid_median_treatment():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_A], "pop": [100]})
    with pytest.raises(ValueError, match="median_treatment must be"):
        harmonize_to_2020_boundaries(
            historical, count_columns=["pop"],
            median_treatment="bogus",  # type: ignore[arg-type]
            crosswalk=cw,
        )


def test_harmonize_invalid_na_treatment():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_A], "pop": [100]})
    with pytest.raises(ValueError, match="na_treatment must be"):
        harmonize_to_2020_boundaries(
            historical, count_columns=["pop"],
            na_treatment="bogus",  # type: ignore[arg-type]
            crosswalk=cw,
        )


def test_harmonize_missing_geography_col_raises():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"WRONG": [_T_A], "pop": [100]})
    with pytest.raises(KeyError, match="geography column"):
        harmonize_to_2020_boundaries(
            historical, count_columns=["pop"], crosswalk=cw,
        )


def test_harmonize_missing_count_col_raises():
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_A], "pop": [100]})
    with pytest.raises(KeyError, match="count columns.*nope"):
        harmonize_to_2020_boundaries(
            historical, count_columns=["nope"], crosswalk=cw,
        )


# ---------------------------------------------------------------------------
# NA treatment modes
# ---------------------------------------------------------------------------

def test_harmonize_na_propagate_default():
    """Default 'propagate': any 2020 tract with an NA contributor becomes NA."""
    cw = _synthetic_crosswalk()
    # M is a merge of M1+M2; NA in M1 should propagate to NA in M
    historical = pd.DataFrame({
        "geoid": [_T_M1, _T_M2], "pop": [pd.NA, 700],
    })
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
        na_treatment="propagate",
    )
    assert pd.isna(out[out["geoid_2020"] == _T_M]["pop"].iloc[0])


def test_harmonize_na_zero_treats_missing_as_zero():
    """'zero': NA M1 contributes 0; M = 0 + 700 = 700. Spurious-shrink risk."""
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({
        "geoid": [_T_M1, _T_M2], "pop": [pd.NA, 700],
    })
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
        na_treatment="zero",
    )
    assert int(out[out["geoid_2020"] == _T_M]["pop"].iloc[0]) == 700


def test_harmonize_na_renormalize_drops_and_renormalizes():
    """'renormalize': drop NA contributors, renormalize surviving weights to 1.

    M1 NA + M2 700: drop M1, M2's weight is renormalized 1.0 -> 1.0
    (alone now), so output = 700 * 1.0 = 700.
    """
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({
        "geoid": [_T_M1, _T_M2], "pop": [pd.NA, 700],
    })
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
        na_treatment="renormalize",
    )
    assert int(out[out["geoid_2020"] == _T_M]["pop"].iloc[0]) == 700


def test_harmonize_na_renormalize_with_split_no_op():
    """Renormalize on a tract with all contributors present is a no-op."""
    cw = _synthetic_crosswalk()
    historical = pd.DataFrame({"geoid": [_T_B], "pop": [1000]})
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw,
        na_treatment="renormalize",
    )
    # Same as default: 50/50 split
    assert int(out[out["geoid_2020"] == _T_B1]["pop"].iloc[0]) == 500
    assert int(out[out["geoid_2020"] == _T_B2]["pop"].iloc[0]) == 500


def test_harmonize_na_renormalize_partial_contributor_loss():
    """In a 3-contributor merge with one NA, renormalize redistributes the
    NA contributor's weight share proportionally onto the surviving two.

    Setup: M_three is a merge of T1, T2, T3 each contributing weight_2010=1.0
    (they each fully feed into M_three). Pop: T1=300, T2=300, T3=NA.
    With renormalize: drop T3, renormalize T1+T2 weights from (1.0+1.0) to
    (0.5+0.5). Output = 300*0.5 + 300*0.5 = 300.
    """
    _T_T1 = "17097000010"
    _T_T2 = "17097000011"
    _T_T3 = "17097000012"
    _T_M3 = "17097000013"
    cw3 = pd.DataFrame({
        "geoid_2010":          [_T_T1, _T_T2, _T_T3],
        "geoid_2020":          [_T_M3] * 3,
        "state_fips":          ["17"] * 3,
        "county_fips":         ["097"] * 3,
        "arealand_part_sqm":   [100, 100, 100],
        "arealand_2020_sqm":   [300, 300, 300],
        "arealand_2010_sqm":   [100, 100, 100],
        "area_weight_2010":    [1.0, 1.0, 1.0],
        "area_weight_2020":    [1/3, 1/3, 1/3],
        "relationship_type":   ["merge"] * 3,
    })
    historical = pd.DataFrame({
        "geoid": [_T_T1, _T_T2, _T_T3],
        "pop":   [300, 300, pd.NA],
    })
    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"], crosswalk=cw3,
        na_treatment="renormalize",
    )
    # T1 + T2 each get weight 0.5 after renorm; output = 300*0.5 + 300*0.5 = 300
    val = float(out[out["geoid_2020"] == _T_M3]["pop"].iloc[0])
    assert abs(val - 300) < 1e-6


# ---------------------------------------------------------------------------
# Real-data integration: Lake County preservation check
# ---------------------------------------------------------------------------

def test_harmonize_lake_county_population_preservation(real_crosswalk):
    """Synthetic 2010-side population for ALL 2010 tracts whose 2020 successors
    are in Lake County, IL; harmonize and verify population total preservation.

    Important: a 2010 tract may straddle the Lake-County border, contributing
    population to both Lake-County 2020 tracts and non-Lake 2020 tracts.
    To get exact total preservation, we must seed populations on every 2010
    tract that contributes to any Lake-County 2020 tract, then sum the OUT
    side over Lake-County 2020 tracts only and weight the IN side by the
    share of each 2010 tract's contribution that fell inside Lake.

    Simpler equivalent: select 2010 tracts that ENTIRELY fall inside Lake
    County 2020 successors (sum of area_weight_2010 to Lake successors == 1.0
    for that 2010 tract). For those, the round-trip is exact.
    """
    # Find 2020 tracts in Lake County, IL
    lake_2020_tracts = set(
        real_crosswalk.loc[
            (real_crosswalk["state_fips"] == "17")
            & (real_crosswalk["county_fips"] == "097"),
            "geoid_2020",
        ].dropna().unique()
    )
    # 2010 tracts whose successors are entirely within Lake County
    cw = real_crosswalk.copy()
    cw["_in_lake"] = cw["geoid_2020"].isin(lake_2020_tracts)
    weight_in_lake = (
        cw.groupby("geoid_2010", observed=True)
          .apply(lambda g: float(
              (g["area_weight_2010"].astype("Float64").fillna(0) * g["_in_lake"]).sum()
          ), include_groups=False)
    )
    fully_in_lake_2010 = set(weight_in_lake[weight_in_lake > 0.999].index)

    # Seed population only on 2010 tracts fully contained in Lake 2020 successors
    interior_2010 = sorted(fully_in_lake_2010)
    rng = np.random.default_rng(seed=42)
    historical = pd.DataFrame({
        "geoid": interior_2010,
        "pop": rng.integers(low=500, high=8000, size=len(interior_2010)),
    })
    total_in = int(historical["pop"].sum())

    out = harmonize_to_2020_boundaries(
        historical, count_columns=["pop"],
        crosswalk=real_crosswalk,
        # 'zero' treats unmatched 2010 contributors (which we did not seed)
        # as 0 contribution, scoping the sum to only the values we seeded.
        na_treatment="zero",
    )
    total_out = float(out["pop"].sum())
    # Allow 0.5% tolerance for residual bleed of seeded 2010 tracts whose
    # area_weight_2010 to Lake 2020 successors was just under 1.0 (the
    # >0.999 cutoff is not exactly 1.0). Real-world boundary harmonization
    # is fundamentally inexact at the cross-county margin.
    assert abs(total_out - total_in) / total_in < 0.005
