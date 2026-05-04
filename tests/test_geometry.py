"""
Tests for the geometry loaders + helpers.

Two distinct surfaces under test:
  - tract-to-PUMA crosswalk: ships in the wheel, always available
  - tract polygons: NOT shipped; runtime-downloaded into a cache dir

For polygon tests we don't hit the network. We synthesize tiny per-state
parquet files in a tmp_path cache and verify the loaders read them
correctly. The actual TIGERweb-fetch path is exercised by the build
script; smoke-testing it would require either mocking requests or
hitting the live release URL, both of which are out of scope here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from telltalere_census.geometry import (
    _all_state_fips,
    _release_url,
    download_tract_polygons,
    load_tract_polygons,
    load_tract_to_puma_crosswalk,
    wkb_to_geojson,
)


# ---------------------------------------------------------------------------
# Crosswalk
# ---------------------------------------------------------------------------

def test_crosswalk_loads_and_has_expected_shape():
    df = load_tract_to_puma_crosswalk()
    assert {"state_fips", "county_fips", "tract_fips", "tract_geoid", "puma_code"} <= set(df.columns)
    assert len(df) > 80_000  # ~85k tracts nationally
    assert df["state_fips"].nunique() >= 52  # 50 + DC + PR


def test_crosswalk_zfill_widths():
    df = load_tract_to_puma_crosswalk(state_fips="6")  # CA, asked as "6" → "06"
    assert (df["state_fips"].str.len() == 2).all()
    assert (df["county_fips"].str.len() == 3).all()
    assert (df["tract_fips"].str.len() == 6).all()
    assert (df["puma_code"].str.len() == 5).all()
    assert (df["tract_geoid"].str.len() == 11).all()
    assert (df["state_fips"] == "06").all()


def test_crosswalk_state_filter_list():
    df = load_tract_to_puma_crosswalk(state_fips=["17", "18"])
    assert set(df["state_fips"].unique()) == {"17", "18"}


def test_crosswalk_lake_county_round_trip():
    # Lake County, IL = 17097. There are many tracts and they should map
    # to the 5 PUMAs we observed during fixture generation.
    df = load_tract_to_puma_crosswalk(state_fips="17")
    lake = df[df["county_fips"] == "097"]
    assert len(lake) > 0
    pumas = set(lake["puma_code"].unique())
    # Sanity: at least the 5 PUMAs we saw during fixture generation
    expected = {"09701", "09702", "09703", "09704", "09705"}
    assert expected <= pumas, f"missing PUMAs: {expected - pumas}"


# ---------------------------------------------------------------------------
# Polygon loaders (synthetic cache, no network)
# ---------------------------------------------------------------------------

def _write_synthetic_state_parquet(cache: Path, state_fips: str, n_tracts: int = 3) -> Path:
    """Tiny stand-in for a TIGERweb-built per-state parquet, with the
    columns the loader contract advertises. polygon_wkb is a placeholder
    bytestring — wkb_to_geojson is not exercised here."""
    df = pd.DataFrame({
        "state_fips":  [state_fips] * n_tracts,
        "county_fips": [f"{i:03d}" for i in range(n_tracts)],
        "tract_fips":  [f"{i:06d}" for i in range(n_tracts)],
        "geoid": [f"{state_fips}{i:03d}{i:06d}" for i in range(n_tracts)],
        "centroid_lon": [-87.6 + i * 0.01 for i in range(n_tracts)],
        "centroid_lat": [42.0 + i * 0.01 for i in range(n_tracts)],
        "polygon_wkb": [b"placeholder"] * n_tracts,
    })
    out = cache / f"tracts_{state_fips}.parquet"
    df.to_parquet(out, index=False)
    return out


def test_load_tract_polygons_single_state(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "17", n_tracts=4)
    df = load_tract_polygons(state_fips="17", cache_dir=cache)
    assert len(df) == 4
    assert (df["state_fips"] == "17").all()


def test_load_tract_polygons_multi_state(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "17", n_tracts=4)
    _write_synthetic_state_parquet(cache, "18", n_tracts=2)
    df = load_tract_polygons(state_fips=["17", "18"], cache_dir=cache)
    assert len(df) == 6
    assert set(df["state_fips"].unique()) == {"17", "18"}


def test_load_tract_polygons_all_cached(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "17", n_tracts=2)
    _write_synthetic_state_parquet(cache, "18", n_tracts=3)
    df = load_tract_polygons(cache_dir=cache)  # None → all cached
    assert len(df) == 5
    assert set(df["state_fips"].unique()) == {"17", "18"}


def test_load_tract_polygons_missing_state_raises_with_helpful_message(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "17", n_tracts=2)
    with pytest.raises(FileNotFoundError) as exc_info:
        load_tract_polygons(state_fips=["17", "18"], cache_dir=cache)
    msg = str(exc_info.value)
    assert "18" in msg
    assert "download_tract_polygons" in msg


def test_load_tract_polygons_empty_cache_raises(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    with pytest.raises(FileNotFoundError, match="No tract polygon parquets"):
        load_tract_polygons(cache_dir=cache)


def test_state_fips_argument_zfills(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "06", n_tracts=2)
    # Caller passes "6" — loader must look up "06"
    df = load_tract_polygons(state_fips="6", cache_dir=cache)
    assert len(df) == 2


def test_state_fips_all_string_normalizes_to_none(tmp_path: Path):
    cache = tmp_path / "geom"
    cache.mkdir()
    _write_synthetic_state_parquet(cache, "17", n_tracts=2)
    _write_synthetic_state_parquet(cache, "18", n_tracts=2)
    df = load_tract_polygons(state_fips="all", cache_dir=cache)
    assert len(df) == 4


# ---------------------------------------------------------------------------
# Release URL composition
# ---------------------------------------------------------------------------

def test_release_url_uses_env_overrides(monkeypatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_RELEASE_OWNER", "telltale-re")
    monkeypatch.setenv("TELLTALERE_CENSUS_RELEASE_TAG", "v9.9.9")
    url = _release_url("17")
    assert url == (
        "https://github.com/telltale-re/telltalere-census/"
        "releases/download/v9.9.9/tracts_17.parquet"
    )


def test_release_url_default_uses_placeholder(monkeypatch):
    monkeypatch.delenv("TELLTALERE_CENSUS_RELEASE_OWNER", raising=False)
    monkeypatch.delenv("TELLTALERE_CENSUS_RELEASE_TAG", raising=False)
    url = _release_url("17")
    assert "REPLACE_ME" in url
    assert "tracts_17.parquet" in url


# ---------------------------------------------------------------------------
# wkb_to_geojson
# ---------------------------------------------------------------------------

def test_wkb_to_geojson_round_trip():
    shapely = pytest.importorskip("shapely")
    from shapely.geometry import Polygon
    poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    wkb = bytes(poly.wkb)
    out = wkb_to_geojson(wkb)
    assert out["type"] == "Polygon"
    # GeoJSON convention: [lon, lat] order
    coords = out["coordinates"][0]
    assert (0, 0) in [tuple(c) for c in coords]


# ---------------------------------------------------------------------------
# State universe
# ---------------------------------------------------------------------------

def test_all_state_fips_includes_dc_and_pr():
    fips = _all_state_fips()
    assert "11" in fips  # DC
    assert "72" in fips  # PR
    assert len(fips) == 52


# ---------------------------------------------------------------------------
# Network-touching downloader smoke (skipped by default)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("TELLTALERE_CENSUS_RELEASE_OWNER", "REPLACE_ME") == "REPLACE_ME",
    reason="No real release configured; download_tract_polygons untestable.",
)
def test_download_tract_polygons_pulls_one_state(tmp_path: Path):
    paths = download_tract_polygons(state_fips="17", cache_dir=tmp_path)
    assert len(paths) == 1
    assert paths[0].exists()
    assert paths[0].stat().st_size > 0
