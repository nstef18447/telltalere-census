"""Unit tests for telltalere_census.multi_vintage."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from telltalere_census.multi_vintage import (
    fetch_acs_data_multi_vintage,
    get_available_vintages,
)


# ---------------------------------------------------------------------------
# get_available_vintages
# ---------------------------------------------------------------------------

def test_acs5_tract_available_range():
    out = get_available_vintages(geography="tract", acs_type="acs5")
    assert out[0] == 2013
    assert out[-1] == 2024
    assert 2020 in out  # 2020 was published as 5-year
    assert sorted(out) == out


def test_acs5_block_group_available_range():
    out = get_available_vintages(geography="block group", acs_type="acs5")
    assert out[0] == 2013
    assert out[-1] == 2024


def test_acs1_excludes_2020():
    out = get_available_vintages(geography="state", acs_type="acs1")
    assert 2020 not in out
    assert 2019 in out
    assert 2021 in out
    assert sorted(out) == out


def test_acs1_starts_2014():
    out = get_available_vintages(geography="state", acs_type="acs1")
    assert out[0] == 2014


def test_acs1_does_not_publish_block_group():
    with pytest.raises(ValueError, match="ACS 1-year does not publish 'block group'"):
        get_available_vintages(geography="block group", acs_type="acs1")


def test_acs1_does_not_publish_tract():
    with pytest.raises(ValueError, match="ACS 1-year does not publish 'tract'"):
        get_available_vintages(geography="tract", acs_type="acs1")


def test_invalid_geography_raises():
    with pytest.raises(ValueError, match="geography must be one of"):
        get_available_vintages(geography="zip code", acs_type="acs5")  # type: ignore[arg-type]


def test_invalid_acs_type_raises():
    with pytest.raises(ValueError, match="acs_type must be"):
        get_available_vintages(geography="tract", acs_type="acs3")  # type: ignore[arg-type]


def test_acs5_county_includes_full_range():
    """County geography is published for both 5-year and 1-year."""
    out_5 = get_available_vintages(geography="county", acs_type="acs5")
    out_1 = get_available_vintages(geography="county", acs_type="acs1")
    assert 2024 in out_5
    assert 2024 in out_1
    assert 2020 not in out_1


# ---------------------------------------------------------------------------
# fetch_acs_data_multi_vintage — mocked single-vintage path
# ---------------------------------------------------------------------------

def _make_synthetic_state_frame(state_fips: str, n_tracts: int = 3) -> pd.DataFrame:
    """3-tract synthetic ACS-shaped DataFrame matching what fetch_acs_data returns."""
    return pd.DataFrame({
        "state": [state_fips] * n_tracts,
        "county": [f"{i:03d}" for i in range(n_tracts)],
        "tract": [f"{i:06d}" for i in range(n_tracts)],
        "GEOID": [f"{state_fips}{i:03d}{i:06d}" for i in range(n_tracts)],
        "B01003_001E": [1000 + i * 100 for i in range(n_tracts)],
        "B01003_001M": [50] * n_tracts,
    })


def test_multi_vintage_stacks_into_long_format():
    """Two vintages with 3 tracts each -> 6-row long-format DataFrame with vintage column."""
    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        return _make_synthetic_state_frame(state_fips)

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        df = fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01003_001E"],
            vintages=[2018, 2023],
            geography="tract",
            acs_type="acs5",
        )

    assert len(df) == 6  # 2 vintages * 3 tracts
    assert "vintage" in df.columns
    assert set(df["vintage"].unique()) == {2018, 2023}
    assert df.loc[df["vintage"] == 2018].shape[0] == 3
    assert df.loc[df["vintage"] == 2023].shape[0] == 3


def test_multi_vintage_sorts_vintages_ascending():
    """Even if input is unsorted, vintages stack ascending."""
    captured_calls: list[int] = []

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        captured_calls.append(vintage)
        return _make_synthetic_state_frame(state_fips)

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01003_001E"],
            vintages=[2023, 2018, 2020],
            geography="tract",
        )

    assert captured_calls == [2018, 2020, 2023]


def test_multi_vintage_deduplicates_vintages():
    """Repeated vintages in input are fetched only once."""
    captured_calls: list[int] = []

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        captured_calls.append(vintage)
        return _make_synthetic_state_frame(state_fips)

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01003_001E"],
            vintages=[2018, 2018, 2023, 2018],
            geography="tract",
        )

    assert captured_calls == [2018, 2023]


def test_multi_vintage_validates_unavailable_vintages():
    """Requesting a vintage outside the available range raises before any fetch."""
    with pytest.raises(ValueError, match="not available"):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            vintages=[2010, 2018],  # 2010 not in 2013+ range
            geography="tract",
        )


def test_multi_vintage_rejects_acs1_block_group():
    """ACS 1-year + block group is rejected before any fetch (via get_available_vintages)."""
    with pytest.raises(ValueError, match="ACS 1-year does not publish 'block group'"):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            vintages=[2019, 2021],
            geography="block group",
            acs_type="acs1",
        )


def test_multi_vintage_rejects_empty_vintages():
    with pytest.raises(ValueError, match="vintages list is required"):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            vintages=[],
            geography="tract",
        )


def test_multi_vintage_rejects_none_vintages():
    with pytest.raises(ValueError, match="vintages list is required"):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            vintages=None,
            geography="tract",
        )


def test_multi_vintage_uses_default_vars_when_none():
    """Default ACS_BG_DEFAULT_VARS is used when variables=None."""
    captured_vars: list[list[str]] = []

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        captured_vars.append(list(variables))
        return _make_synthetic_state_frame(state_fips)

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            vintages=[2018],
            geography="tract",
        )

    # Default variable list is the package's ACS_BG_DEFAULT_VARS (~85 vars)
    assert "B01003_001E" in captured_vars[0]
    assert len(captured_vars[0]) > 50  # Sanity: full default list is large


def test_multi_vintage_passes_through_kwargs():
    """include_moe, cache_dir, geography, acs_type are forwarded to fetch_acs_data."""
    captured_calls: list[dict] = []

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        captured_calls.append({
            "state_fips": state_fips,
            "geography": geography,
            "vintage": vintage,
            "acs_type": acs_type,
            "include_moe": include_moe,
            "cache_dir": cache_dir,
        })
        return _make_synthetic_state_frame(state_fips)

    from pathlib import Path
    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01003_001E"],
            vintages=[2018],
            geography="block group",
            acs_type="acs5",
            include_moe=False,
            cache_dir=Path("/tmp/whatever"),
        )

    call = captured_calls[0]
    assert call["geography"] == "block group"
    assert call["acs_type"] == "acs5"
    assert call["include_moe"] is False
    assert call["cache_dir"] == Path("/tmp/whatever")
