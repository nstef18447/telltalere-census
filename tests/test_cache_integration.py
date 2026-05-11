"""Bulk-cache-first read path: fetch_acs_data_multi_vintage reads from the
warm cache when present and skips the live API entirely."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from telltalere_census.multi_vintage import (
    _try_bulk_cache_read,
    fetch_acs_data_multi_vintage,
)
from telltalere_census.warm_cache import bulk_parquet_path


def _write_bulk_parquet(
    cache_root: Path, geography: str, vintage_tag: str, table: str,
    state_fips: str, n_rows: int = 5, extra_table_cols: bool = True,
) -> Path:
    """Synthesize a parquet that looks like a bulk-warmed group(<table>) result."""
    p = bulk_parquet_path(cache_root, geography, vintage_tag, table, state_fips)
    p.parent.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({
        "state": [state_fips] * n_rows,
        "county": ["031"] * n_rows,
        "tract": [f"{i:06d}" for i in range(n_rows)],
        "GEOID": [f"{state_fips}031{i:06d}" for i in range(n_rows)],
        f"{table}_001E": list(range(100, 100 + n_rows)),
        f"{table}_001M": [10] * n_rows,
    })
    if extra_table_cols:
        df[f"{table}_002E"] = list(range(200, 200 + n_rows))
        df[f"{table}_002M"] = [20] * n_rows
    df.to_parquet(p, index=False)
    return p


# ---------------------------------------------------------------------------
# _try_bulk_cache_read
# ---------------------------------------------------------------------------

def test_bulk_cache_read_returns_filtered_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B01001", "17")

    df = _try_bulk_cache_read(
        state_fips="17",
        requested_vars=["B01001_001E"],
        geography="tract", vintage=2024, acs_type="acs5", include_moe=True,
    )
    assert df is not None
    assert "B01001_001E" in df.columns
    assert "B01001_001M" in df.columns  # MOE auto-pair
    # _002E should NOT be in the filtered slice
    assert "B01001_002E" not in df.columns
    assert "GEOID" in df.columns
    assert len(df) == 5


def test_bulk_cache_read_returns_none_when_missing_parquet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    df = _try_bulk_cache_read(
        state_fips="17",
        requested_vars=["B01001_001E"],
        geography="tract", vintage=2024, acs_type="acs5", include_moe=True,
    )
    assert df is None


def test_bulk_cache_read_handles_cross_table_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B01001", "17")
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B19001", "17")

    df = _try_bulk_cache_read(
        state_fips="17",
        requested_vars=["B01001_001E", "B19001_001E"],
        geography="tract", vintage=2024, acs_type="acs5", include_moe=False,
    )
    assert df is not None
    assert {"B01001_001E", "B19001_001E"}.issubset(df.columns)
    assert "B01001_001M" not in df.columns  # include_moe=False
    assert len(df) == 5  # inner join on identical GEOIDs


def test_bulk_cache_read_returns_none_for_block_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """BG is not in bulk warm scope; always fall through."""
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    # Even with a tract parquet present, BG requests fall through.
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B01001", "17")
    df = _try_bulk_cache_read(
        state_fips="17", requested_vars=["B01001_001E"],
        geography="block group", vintage=2024, acs_type="acs5", include_moe=True,
    )
    assert df is None


def test_bulk_cache_read_returns_none_for_acs1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ACS1 tract isn't possible (Census doesn't publish), but explicitly check."""
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    df = _try_bulk_cache_read(
        state_fips="17", requested_vars=["B01001_001E"],
        geography="tract", vintage=2024, acs_type="acs1", include_moe=True,
    )
    assert df is None


# ---------------------------------------------------------------------------
# fetch_acs_data_multi_vintage with bulk cache hit
# ---------------------------------------------------------------------------

def test_multi_vintage_uses_bulk_cache_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A complete bulk-cache hit should produce results without calling the API."""
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B01001", "17")

    with patch("telltalere_census.multi_vintage.fetch_acs_data") as mock_fetch:
        mock_fetch.side_effect = AssertionError("should NOT have called fetch_acs_data")
        df = fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01001_001E"],
            vintages=[2024],
            geography="tract",
            acs_type="acs5",
            include_moe=True,
        )
    assert "vintage" in df.columns
    assert (df["vintage"] == 2024).all()
    assert "B01001_001E" in df.columns
    assert "B01001_001M" in df.columns
    assert len(df) == 5


def test_multi_vintage_falls_through_on_partial_bulk_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If any requested table is missing in bulk, the API path runs for the whole vintage."""
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))
    _write_bulk_parquet(tmp_path, "tract", "2020-2024", "B01001", "17")
    # B19001 NOT pre-warmed.

    api_call_log: list[dict] = []

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        api_call_log.append({"vintage": vintage, "variables": list(variables)})
        return pd.DataFrame({
            "GEOID": ["17031000100"], "B01001_001E": [1], "B19001_001E": [2],
        })

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        df = fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01001_001E", "B19001_001E"],
            vintages=[2024],
            geography="tract",
            acs_type="acs5",
            include_moe=False,
        )
    assert len(api_call_log) == 1  # one API call covering the missing table
    assert df.shape[0] == 1


def test_multi_vintage_block_group_always_uses_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """BG path should never touch the bulk cache."""
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path))

    api_called = {"n": 0}

    def fake_fetch(state_fips, variables, geography, vintage, acs_type, include_moe, cache_dir):
        api_called["n"] += 1
        return pd.DataFrame({"GEOID": ["170310001001"], "B01001_001E": [99]})

    with patch("telltalere_census.multi_vintage.fetch_acs_data", side_effect=fake_fetch):
        fetch_acs_data_multi_vintage(
            state_fips="17",
            variables=["B01001_001E"],
            vintages=[2024],
            geography="block group",
            acs_type="acs5",
            include_moe=False,
        )
    assert api_called["n"] == 1
