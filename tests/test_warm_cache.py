"""Unit tests for telltalere_census.warm_cache."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from telltalere_census.warm_cache import (
    DEFAULT_WARM_TABLES,
    NATION_WIDE_GEOGRAPHIES,
    WarmCacheResult,
    _base_url,
    _build_api_params,
    _build_dataframe,
    _enumerate_tuples,
    _PermanentHTTPError,
    _RateLimiter,
    _RetryableHTTPError,
    _WarmTuple,
    _with_retry,
    bulk_parquet_path,
    parse_vintage_tag_5yr,
    resolve_bulk_cache_dir,
    vintage_tag_1yr,
    vintage_tag_5yr,
    warm_cache,
)


# Synthetic key used in tests so warm_cache's startup check passes without
# touching the live Census API.
_TEST_API_KEY = "test-key-for-unit-tests"


# ---------------------------------------------------------------------------
# Synthetic API response builders
# ---------------------------------------------------------------------------

def _fake_tract_response(table: str, n_rows: int = 3) -> list[list]:
    """Build a Census-API-shaped JSON response for tract geography."""
    headers = [
        f"{table}_001E", f"{table}_001EA", f"{table}_001M", f"{table}_001MA",
        f"{table}_002E", f"{table}_002EA", f"{table}_002M", f"{table}_002MA",
        "state", "county", "tract",
    ]
    rows = []
    for i in range(n_rows):
        rows.append([
            str(1000 + i), "", "100", "",       # _001E, _001EA, _001M, _001MA
            str(500 + i), "", "50", "",          # _002E, _002EA, _002M, _002MA
            "17", "031", f"{i:06d}",
        ])
    return [headers] + rows


def _fake_response(table: str, status: int = 200, payload: Optional[list] = None):
    r = MagicMock()
    r.status_code = status
    r.text = "" if status == 200 else f"error {status}"
    r.json.return_value = payload if payload is not None else _fake_tract_response(table)
    r.raise_for_status = MagicMock()
    return r


# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

def test_default_warm_tables_count_matches_spec_explicit_list():
    """Spec lists 4+3+4+9+1+3 = 24 B-tables + 4 DP = 28 tables explicitly."""
    assert len(DEFAULT_WARM_TABLES) == 28
    assert all(t.startswith("B") or t.startswith("DP") for t in DEFAULT_WARM_TABLES)


def test_vintage_tag_5yr_roundtrip():
    assert vintage_tag_5yr(2024) == "2020-2024"
    assert vintage_tag_5yr(2017) == "2013-2017"
    assert parse_vintage_tag_5yr("2020-2024") == 2024
    assert parse_vintage_tag_5yr("2013-2017") == 2017


def test_vintage_tag_1yr_is_str_year():
    assert vintage_tag_1yr(2024) == "2024"
    assert vintage_tag_1yr(2014) == "2014"


def test_resolve_bulk_cache_dir_uses_explicit(tmp_path: Path):
    out = resolve_bulk_cache_dir(tmp_path / "explicit")
    assert out == tmp_path / "explicit"
    assert out.exists()


def test_resolve_bulk_cache_dir_uses_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELLTALERE_CENSUS_BULK_CACHE_DIR", str(tmp_path / "via_env"))
    out = resolve_bulk_cache_dir()
    assert out == tmp_path / "via_env"
    assert out.exists()


def test_bulk_parquet_path_layout(tmp_path: Path):
    p = bulk_parquet_path(tmp_path, "tract", "2020-2024", "B01001", "17")
    assert p == tmp_path / "tract" / "2020-2024" / "B01001_17.parquet"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_enforces_min_interval():
    """Three acquires at 10/sec should take at least ~0.2s (two intervals)."""
    rl = _RateLimiter(target_rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(3):
        rl.acquire()
    elapsed = time.monotonic() - start
    # Two enforced gaps of 0.1s each = 0.2s minimum.
    assert elapsed >= 0.15, f"rate limiter not enforcing min interval (elapsed={elapsed:.3f}s)"


def test_rate_limiter_zero_rate_is_no_op():
    rl = _RateLimiter(target_rate_per_sec=0.0)
    start = time.monotonic()
    for _ in range(5):
        rl.acquire()
    assert time.monotonic() - start < 0.01


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def test_with_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _RetryableHTTPError(503, "transient")
        return "ok"

    out = _with_retry(flaky, max_attempts=3, base_delay=0.01, max_delay=0.05)
    assert out == "ok"
    assert calls["n"] == 2


def test_with_retry_raises_after_max_attempts():
    def always_fail():
        raise _RetryableHTTPError(503, "transient")

    with pytest.raises(_RetryableHTTPError):
        _with_retry(always_fail, max_attempts=2, base_delay=0.01, max_delay=0.02)


def test_with_retry_does_not_retry_permanent_error():
    """4xx is _PermanentHTTPError; _with_retry only catches _RetryableHTTPError."""
    calls = {"n": 0}

    def fail_permanent():
        calls["n"] += 1
        raise _PermanentHTTPError(400, "bad request")

    with pytest.raises(_PermanentHTTPError):
        _with_retry(fail_permanent, max_attempts=3, base_delay=0.01)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Enumerate tuples
# ---------------------------------------------------------------------------

def test_enumerate_tuples_state_bound_and_nationwide_counts():
    tuples = _enumerate_tuples(
        vintages_5yr=["2020-2024"],
        vintages_1yr=[2024],
        geographies_5yr=["tract", "msa"],
        geographies_1yr=["state", "msa"],
        states=["17", "06"],
        tables=["B01001"],
    )
    # 5yr tract: 2 states × 1 table × 1 vintage = 2
    # 5yr msa: 1 nation-wide × 1 table × 1 vintage = 1
    # 1yr state: 2 states × 1 table × 1 vintage = 2
    # 1yr msa: 1 × 1 × 1 = 1
    assert len(tuples) == 6
    state_bound = [t for t in tuples if t.geography in {"tract", "state"}]
    nation_wide = [t for t in tuples if t.geography == "msa"]
    assert len(state_bound) == 4
    assert len(nation_wide) == 2
    assert all(t.state_fips == "00" for t in nation_wide)


def test_enumerate_tuples_carries_acs_type_and_year():
    tuples = _enumerate_tuples(
        vintages_5yr=["2020-2024"], vintages_1yr=[2024],
        geographies_5yr=["tract"], geographies_1yr=["state"],
        states=["17"], tables=["B01001"],
    )
    by_type = {t.acs_type: t for t in tuples}
    assert by_type["acs5"].vintage_year == 2024
    assert by_type["acs5"].vintage_tag == "2020-2024"
    assert by_type["acs1"].vintage_year == 2024
    assert by_type["acs1"].vintage_tag == "2024"


# ---------------------------------------------------------------------------
# _build_dataframe — sentinel handling, typing, GEOID synthesis
# ---------------------------------------------------------------------------

def test_build_dataframe_creates_geoid_for_tract():
    raw = _fake_tract_response("B01001")
    df = _build_dataframe(raw, "tract")
    assert "GEOID" in df.columns
    assert df["GEOID"].iloc[0] == "17031000000"
    assert df["B01001_001E"].dtype.name == "Int64"
    assert df["B01001_001M"].dtype.name == "Int64"


def test_build_dataframe_handles_string_sentinels():
    raw = [
        ["B01001_001E", "B01001_001M", "state", "county", "tract"],
        ["100", "", "17", "031", "000100"],
        ["", "(X)", "17", "031", "000200"],
        ["200", "**", "17", "031", "000300"],
    ]
    df = _build_dataframe(raw, "tract")
    # Row 0 should be 100/NA-MOE; row 1: NA/NA; row 2: 200/NA
    assert df["B01001_001E"].iloc[0] == 100
    assert pd.isna(df["B01001_001E"].iloc[1])
    assert pd.isna(df["B01001_001M"].iloc[0])  # "" sentinel
    assert pd.isna(df["B01001_001M"].iloc[2])  # "**" sentinel


def test_build_dataframe_handles_integer_sentinel():
    raw = [
        ["B25064_001E", "B25064_001M", "state", "county", "tract"],
        ["1500", "50", "17", "031", "000100"],
        ["-666666666", "-555555555", "17", "031", "000200"],
    ]
    df = _build_dataframe(raw, "tract")
    assert df["B25064_001E"].iloc[0] == 1500
    assert pd.isna(df["B25064_001E"].iloc[1])
    assert pd.isna(df["B25064_001M"].iloc[1])


def test_build_dataframe_preserves_annotation_string_columns():
    raw = [
        ["B01001_001E", "B01001_001EA", "state", "county", "tract"],
        ["100", "X", "17", "031", "000100"],
    ]
    df = _build_dataframe(raw, "tract")
    # EA column is preserved as string, not coerced
    assert "B01001_001EA" in df.columns
    # We do not coerce annotation columns; pandas will read it as object/string
    assert df["B01001_001EA"].iloc[0] == "X"


# ---------------------------------------------------------------------------
# Integration: warm_cache with mocked HTTP, single-state small batch
# ---------------------------------------------------------------------------

def _make_mock_get(table_responses: dict[str, list]):
    """Return a side_effect for requests.get keyed by table param."""

    def side_effect(url, params=None, timeout=None):
        params = params or {}
        get_param = params.get("get", "")
        # `group(BXXXXX)` → table = "BXXXXX"
        table = get_param.replace("group(", "").replace(")", "")
        if table not in table_responses:
            return _fake_response(table, status=404, payload=[])
        return _fake_response(table, status=200, payload=table_responses[table])

    return side_effect


def test_warm_cache_writes_parquets(tmp_path: Path):
    table_responses = {
        "B01001": _fake_tract_response("B01001"),
        "B19001": _fake_tract_response("B19001"),
    }
    with patch("telltalere_census.warm_cache.requests") as mock_requests:
        mock_requests.get.side_effect = _make_mock_get(table_responses)
        # Need to expose the right exception types for code paths that catch them
        mock_requests.RequestException = Exception

        result = warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["tract"],
            geographies_1yr=[],
            states=["17"],
            tables=["B01001", "B19001"],
            max_workers=2,
            target_rate_per_sec=100.0,  # fast for tests
            min_free_gb=0.0,
            api_key=_TEST_API_KEY,
        )

    assert isinstance(result, WarmCacheResult)
    assert result.completed_count == 2
    assert result.failed_count == 0
    assert (tmp_path / "tract" / "2020-2024" / "B01001_17.parquet").exists()
    assert (tmp_path / "tract" / "2020-2024" / "B19001_17.parquet").exists()


def test_warm_cache_is_resumable(tmp_path: Path):
    """Pre-populate one parquet; a second run skips it."""
    pre = tmp_path / "tract" / "2020-2024" / "B01001_17.parquet"
    pre.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"GEOID": ["17031000100"], "B01001_001E": [42]}).to_parquet(pre, index=False)

    call_log: list[str] = []

    def tracking_get(url, params=None, timeout=None):
        call_log.append(params.get("get", ""))
        return _fake_response("B19001", payload=_fake_tract_response("B19001"))

    with patch("telltalere_census.warm_cache.requests") as mock_requests:
        mock_requests.get.side_effect = tracking_get
        mock_requests.RequestException = Exception

        result = warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["tract"],
            geographies_1yr=[],
            states=["17"],
            tables=["B01001", "B19001"],
            max_workers=1,
            target_rate_per_sec=100.0,
            min_free_gb=0.0,
            api_key=_TEST_API_KEY,
        )

    assert result.skipped_count == 1
    assert result.completed_count == 1
    # Only B19001 should have hit the API
    assert all("B01001" not in c for c in call_log)
    assert any("B19001" in c for c in call_log)


def test_warm_cache_dry_run_does_not_fetch(tmp_path: Path):
    with patch("telltalere_census.warm_cache.requests") as mock_requests:
        mock_requests.get.side_effect = AssertionError("dry-run should not call API")
        mock_requests.RequestException = Exception

        result = warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["tract"],
            geographies_1yr=[],
            states=["17"],
            tables=["B01001"],
            max_workers=1,
            target_rate_per_sec=100.0,
            min_free_gb=0.0,
            dry_run=True,
            api_key=_TEST_API_KEY,
        )
    assert result.completed_count == 0
    assert result.failed_count == 0


def test_warm_cache_logs_permanent_failure(tmp_path: Path):
    """4xx should not retry; tuple lands in failed_tuples."""

    def always_400(url, params=None, timeout=None):
        return _fake_response("B01001", status=400, payload=[])

    with patch("telltalere_census.warm_cache.requests") as mock_requests:
        mock_requests.get.side_effect = always_400
        mock_requests.RequestException = Exception

        result = warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["tract"],
            geographies_1yr=[],
            states=["17"],
            tables=["B01001"],
            max_workers=1,
            target_rate_per_sec=100.0,
            min_free_gb=0.0,
            api_key=_TEST_API_KEY,
        )

    assert result.failed_count == 1
    assert result.completed_count == 0


def test_warm_cache_retries_transient_failure(tmp_path: Path):
    """503 once then 200 → completes."""
    call_state = {"n": 0}

    def transient_then_ok(url, params=None, timeout=None):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return _fake_response("B01001", status=503, payload=[])
        return _fake_response("B01001", payload=_fake_tract_response("B01001"))

    with patch("telltalere_census.warm_cache.requests") as mock_requests:
        mock_requests.get.side_effect = transient_then_ok
        mock_requests.RequestException = Exception

        result = warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["tract"],
            geographies_1yr=[],
            states=["17"],
            tables=["B01001"],
            max_workers=1,
            target_rate_per_sec=100.0,
            min_free_gb=0.0,
            api_key=_TEST_API_KEY,
        )

    assert result.completed_count == 1
    assert result.failed_count == 0
    assert call_state["n"] >= 2


def test_warm_cache_rejects_invalid_geography(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid geographies"):
        warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"],
            vintages_1yr=[],
            geographies_5yr=["block group"],  # not in scope
            geographies_1yr=[],
            states=["17"], tables=["B01001"],
            max_workers=1, target_rate_per_sec=100.0, min_free_gb=0.0,
            api_key=_TEST_API_KEY,
        )


# ---------------------------------------------------------------------------
# Missing-API-key guard (regression: v0.4.0 silently produced ~6,000 keyless
# requests that the Census API redirected to an HTML "missing key" page,
# which `resp.json()` then choked on with "Expecting value: line 2 column 1").
# ---------------------------------------------------------------------------

def test_warm_cache_raises_when_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Loud failure beats silently fetching 6,000 missing-key HTML pages."""
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Census API key"):
        warm_cache(
            cache_dir=tmp_path,
            vintages_5yr=["2020-2024"], vintages_1yr=[],
            geographies_5yr=["tract"], geographies_1yr=[],
            states=["17"], tables=["B01001"],
            max_workers=1, target_rate_per_sec=100.0, min_free_gb=0.0,
            dry_run=True,
        )


def test_warm_cache_accepts_key_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """CENSUS_API_KEY env var satisfies the key requirement."""
    monkeypatch.setenv("CENSUS_API_KEY", _TEST_API_KEY)
    # dry_run avoids any API call; we only care that the guard passes.
    result = warm_cache(
        cache_dir=tmp_path,
        vintages_5yr=["2020-2024"], vintages_1yr=[],
        geographies_5yr=["tract"], geographies_1yr=[],
        states=["17"], tables=["B01001"],
        max_workers=1, target_rate_per_sec=100.0, min_free_gb=0.0,
        dry_run=True,
    )
    assert result.completed_count == 0


# ---------------------------------------------------------------------------
# URL construction regression tests.
#
# These pin the exact URL string the warmer sends to the Census API for each
# geography × table-prefix combination. A real URL bug would change the
# resulting URL and one of these tests would fail before any live API call.
#
# Canonical patterns verified 2026-05-12 against ACS5 2017 with a valid key.
# ---------------------------------------------------------------------------

def _build_url_for(
    table: str, geography: str, state_fips, vintage_year: int = 2017,
    acs_type: str = "acs5",
) -> str:
    """Build the same URL string the warmer would send, sans api_key."""
    import requests as _r
    base = _base_url(vintage_year, acs_type, table)
    params = _build_api_params(table, geography, state_fips, api_key=None)
    return _r.Request("GET", base, params=params).prepare().url


def test_base_url_routes_b_tables_to_base_endpoint():
    assert _base_url(2017, "acs5", "B19013") == "https://api.census.gov/data/2017/acs/acs5"
    assert _base_url(2024, "acs5", "B01001") == "https://api.census.gov/data/2024/acs/acs5"


def test_base_url_routes_dp_tables_to_profile_endpoint():
    """DP-tables (data profiles) live at /acs/acs5/profile, not /acs/acs5."""
    assert _base_url(2017, "acs5", "DP05") == "https://api.census.gov/data/2017/acs/acs5/profile"
    assert _base_url(2024, "acs5", "DP03") == "https://api.census.gov/data/2024/acs/acs5/profile"


def test_base_url_routes_s_tables_to_subject_endpoint():
    assert _base_url(2017, "acs5", "S0101") == "https://api.census.gov/data/2017/acs/acs5/subject"


def test_url_construction_puma_b_table_2017_il():
    """Probe 1: regression for the 'PUMA URL construction' false-alarm bug.

    Canonical pattern (verified live 2026-05-12, returned 88 rows):
      https://api.census.gov/data/2017/acs/acs5
        ?get=group(B19013)
        &for=public+use+microdata+area:*
        &in=state:17
    """
    url = _build_url_for("B19013", "puma", "17")
    assert url == (
        "https://api.census.gov/data/2017/acs/acs5"
        "?get=group%28B19013%29"
        "&for=public+use+microdata+area%3A%2A"
        "&in=state%3A17"
    )


def test_url_construction_tract_dp_table_2017_il():
    """Probe 3: regression for the 'tract+DP routing' false-alarm bug.

    Canonical pattern (verified live 2026-05-12, returned 3,123 rows):
      https://api.census.gov/data/2017/acs/acs5/profile
        ?get=group(DP05)
        &for=tract:*
        &in=state:17
    """
    url = _build_url_for("DP05", "tract", "17")
    assert url == (
        "https://api.census.gov/data/2017/acs/acs5/profile"
        "?get=group%28DP05%29"
        "&for=tract%3A%2A"
        "&in=state%3A17"
    )


def test_url_construction_tract_b_table_2017_il():
    """Working tract + B-table reference pattern (verified 3,123 rows)."""
    url = _build_url_for("B19013", "tract", "17")
    assert url == (
        "https://api.census.gov/data/2017/acs/acs5"
        "?get=group%28B19013%29"
        "&for=tract%3A%2A"
        "&in=state%3A17"
    )


def test_url_construction_county_and_place_state_bound():
    """County and place share the state-bound URL shape with tract."""
    assert _build_url_for("B01001", "county", "17") == (
        "https://api.census.gov/data/2017/acs/acs5"
        "?get=group%28B01001%29"
        "&for=county%3A%2A"
        "&in=state%3A17"
    )
    assert _build_url_for("B01001", "place", "17") == (
        "https://api.census.gov/data/2017/acs/acs5"
        "?get=group%28B01001%29"
        "&for=place%3A%2A"
        "&in=state%3A17"
    )


def test_url_construction_state_geography_inlines_fips():
    """State geography uses for=state:NN (no separate in= clause)."""
    params = _build_api_params("B01001", "state", "17", api_key=None)
    assert params["for"] == "state:17"
    assert "in" not in params


def test_url_construction_nationwide_geographies_have_no_in_clause():
    """MSA and ZCTA queries don't take a state filter."""
    for geo, expected_for in [
        ("msa", "metropolitan statistical area/micropolitan statistical area:*"),
        ("zcta", "zip code tabulation area:*"),
    ]:
        params = _build_api_params("B01001", geo, state_fips=None, api_key=None)
        assert params["for"] == expected_for
        assert "in" not in params, f"{geo} should not have an in= clause"


def test_build_api_params_passes_key_when_provided():
    params = _build_api_params("B19013", "tract", "17", api_key="hunter2")
    assert params["key"] == "hunter2"


def test_build_api_params_omits_key_when_none():
    """The omission is fine for unit tests; the warm_cache() entry point
    enforces that a key is always supplied before any request is built."""
    params = _build_api_params("B19013", "tract", "17", api_key=None)
    assert "key" not in params
