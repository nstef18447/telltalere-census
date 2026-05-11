"""
Multi-vintage ACS5 / ACS1 fetch — thin wrapper over `acs_fetch.fetch_acs_data`.

The single-vintage path handles caching, chunking, MOE pairing, and
sentinel normalization. This module simply loops over vintages, fetches
each one through the existing path, and stacks the results into a long-
format DataFrame with a `vintage` column.

Each (vintage, state, geography) parquet remains independently cached
under the existing scheme — there is no "multi-vintage" parquet on
disk. This means callers that progressively widen the vintage list pay
a cache miss only on the *new* vintages.

Available vintages
------------------
Census's published-vintage cadence is stable but discrete; the
hardcoded availability lists here reflect what was published as of
the last-verified date in `get_available_vintages` and need an annual
refresh when Census ships a new vintage. ACS 1-year skipped 2020 due
to COVID data-quality issues (Census released only experimental tables
for that year).

Inflation-comparability caveat
------------------------------
Trend analysis on income-bracket variables (B25118, B19001, etc.) and
dollar-denominated variables (B25064 median rent, B25077 median home
value, etc.) across vintages is over **vintage-native nominal dollars**.
ACS does not adjust historical estimates for inflation; a "$100,000+"
bracket in 2014-2018 represents different real purchasing power than
the same nominal label in 2019-2023. Cross-vintage comparison of
nominal income brackets is mathematically valid but economically
misleading without a CPI deflator.

This package does not ship CPI deflation. Real-dollar harmonization is
explicitly out of scope as of v0.3.0 — consumers should apply their own
deflator (e.g., BLS CPI-U-RS series) at their own layer if real-dollar
comparison is required. Population counts, tenure counts, and bracket
*shares* (computed as a percent of the row total) are unaffected by
inflation.

Boundary changes
----------------
Tract / block-group boundaries differ between the 2010 Census and 2020
Census. ACS 5-year vintages with end-year <= 2019 use 2010 boundaries;
end-year >= 2020 use 2020 boundaries. ACS 1-year vintages with
end-year <= 2019 use 2010 boundaries; end-year >= 2021 use 2020
boundaries (2020 1-year was not published).

To compare tract-level data across the boundary cutover, harmonize the
historical (2010-boundary) subset to 2020 boundaries via
`boundaries.harmonize_to_2020_boundaries` before trend computation.
The multi-vintage fetcher does NOT auto-harmonize — boundary
differences are surfaced in the long-format output as multiple
GEOIDs per nominal location.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import pandas as pd

from telltalere_census.acs_fetch import fetch_acs_data
from telltalere_census.variables import ACS_BG_DEFAULT_VARS


# ---------------------------------------------------------------------------
# Vintage availability
# ---------------------------------------------------------------------------

# Last verified against api.census.gov on 2026-05-04. Refresh annually.
#
# Source URLs:
#   ACS 5-year:  https://www.census.gov/data/developers/data-sets/acs-5year.html
#   ACS 1-year:  https://www.census.gov/data/developers/data-sets/acs-1year.html
#
# Modern variable-code stability for the package's default variable list
# (B25118, B25007, B25009 in current shape) starts at 2013 end-year for
# ACS 5-year. ACS 1-year first usable vintage is 2014. 2020 1-year is
# excluded — Census did not publish it (COVID collection issues).
_AVAILABLE_VINTAGES_ACS5: list[int] = list(range(2013, 2024 + 1))
_AVAILABLE_VINTAGES_ACS1: list[int] = [
    y for y in range(2014, 2024 + 1) if y != 2020
]


def get_available_vintages(
    geography: Literal["block group", "tract", "county", "state", "puma"] = "tract",
    acs_type: Literal["acs5", "acs1"] = "acs5",
) -> list[int]:
    """Return a sorted list of available ACS vintage end-years.

    Hardcoded lists (last verified 2026-05-04). Refresh annually.

    Geography availability rules:
      - block group:  ACS 5-year only (1-year does not publish BG estimates).
      - tract:        ACS 5-year only (1-year does not publish tract estimates).
      - county:       both 5-year and 1-year (subject to 65k pop threshold for 1-year).
      - state, puma:  both 5-year and 1-year.

    Returns:
        Sorted list of vintage end-years. ACS 1-year excludes 2020.

    Raises:
        ValueError: if (geography, acs_type) combination is not valid (e.g.,
            "block group" with "acs1").
    """
    valid_geographies = {"block group", "tract", "county", "state", "puma"}
    if geography not in valid_geographies:
        raise ValueError(
            f"geography must be one of {sorted(valid_geographies)}, got {geography!r}"
        )
    if acs_type not in ("acs5", "acs1"):
        raise ValueError(f"acs_type must be 'acs5' or 'acs1', got {acs_type!r}")

    if acs_type == "acs1" and geography in ("block group", "tract"):
        raise ValueError(
            f"ACS 1-year does not publish {geography!r} estimates; use acs5"
        )

    return list(_AVAILABLE_VINTAGES_ACS5 if acs_type == "acs5" else _AVAILABLE_VINTAGES_ACS1)


# ---------------------------------------------------------------------------
# Multi-vintage fetch
# ---------------------------------------------------------------------------

def fetch_acs_data_multi_vintage(
    state_fips: str,
    variables: Optional[list[str]] = None,
    vintages: Optional[list[int]] = None,
    geography: Literal["block group", "tract"] = "block group",
    acs_type: Literal["acs5", "acs1"] = "acs5",
    include_moe: bool = True,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Fetch multiple ACS vintages and return as a long-format DataFrame.

    Each (vintage, state, geography) triple is fetched independently via
    `fetch_acs_data` and the results are stacked. Cache hits per vintage
    are reused; cache misses trigger a single-vintage fetch for that
    vintage only.

    A `vintage` column (int, end-year) is added to the output.

    Args:
        state_fips: 2-digit state FIPS.
        variables: list of estimate variable codes. If None, uses
            ACS_BG_DEFAULT_VARS (same default as fetch_acs_data).
        vintages: list of vintage end-years. Each must appear in
            get_available_vintages(geography, acs_type). Required.
        geography: 'block group' or 'tract'.
        acs_type: 'acs5' (default) or 'acs1'. Note: BG and tract are
            only available for acs5 — passing acs1 with BG/tract raises
            via get_available_vintages.
        include_moe: auto-pair MOE columns. Default True.
        cache_dir: explicit cache directory (overrides env var / default).

    Returns:
        DataFrame in long format. Columns:
          - 'vintage' (int): end-year of the ACS period
          - 'GEOID', 'state', 'county', 'tract' (and 'block_group' for BG)
          - one column per requested estimate variable
          - one column per MOE companion if include_moe=True

        Rows: one per (vintage, geography) combination. The same GEOID
        appears once per vintage. Geographies that exist in some
        vintages but not others (e.g., post-2020 boundary tracts) are
        present only in the rows where they exist — there is no implicit
        zero-fill or boundary-bridging.

    Raises:
        ValueError: if vintages is None or empty, or contains a vintage
            not available for (geography, acs_type).

    Inflation-comparability caveat
    ------------------------------
    Income-bracket and dollar-denominated variables are reported in
    vintage-year nominal dollars. See module docstring for full
    discussion. Apply CPI deflator at the consumer layer if real-dollar
    comparison is required.

    Boundary-change caveat
    ----------------------
    Tract / block-group boundaries changed between the 2010 and 2020
    Census. ACS 5-year vintages with end-year <= 2019 use 2010
    boundaries; end-year >= 2020 use 2020. To compare across the cutover,
    harmonize the historical subset via
    `boundaries.harmonize_to_2020_boundaries` before trend computation.
    """
    if vintages is None or len(vintages) == 0:
        raise ValueError("vintages list is required (received None or empty)")

    available = get_available_vintages(geography=geography, acs_type=acs_type)
    invalid = [v for v in vintages if v not in available]
    if invalid:
        raise ValueError(
            f"vintage(s) {invalid} not available for ({geography!r}, {acs_type!r}). "
            f"Available: {available}"
        )

    requested_vars = list(variables) if variables is not None else list(ACS_BG_DEFAULT_VARS)

    frames: list[pd.DataFrame] = []
    for vintage in sorted(set(vintages)):
        # Bulk-cache fast path: if a warm cache has the union of needed tables
        # for this (state, vintage, geography), assemble the slice from parquets
        # and skip the API entirely. Falls through on any miss.
        df = _try_bulk_cache_read(
            state_fips=state_fips,
            requested_vars=requested_vars,
            geography=geography,
            vintage=vintage,
            acs_type=acs_type,
            include_moe=include_moe,
        )
        if df is None:
            df = fetch_acs_data(
                state_fips=state_fips,
                variables=requested_vars,
                geography=geography,
                vintage=vintage,
                acs_type=acs_type,
                include_moe=include_moe,
                cache_dir=cache_dir,
            )
        df = df.copy()
        df["vintage"] = int(vintage)
        frames.append(df)

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Bulk-cache fast path
# ---------------------------------------------------------------------------

def _try_bulk_cache_read(
    state_fips: str,
    requested_vars: list[str],
    geography: str,
    vintage: int,
    acs_type: str,
    include_moe: bool,
) -> Optional[pd.DataFrame]:
    """Attempt to assemble the requested slice from bulk-cache parquets.

    Returns the DataFrame on a complete hit, or None to signal fall-through
    to the live-API path. Only operates on (geography='tract', acs_type='acs5')
    today — that's the only combo the bulk warmer currently produces for
    sub-county data.
    """
    if geography != "tract" or acs_type != "acs5":
        return None

    from telltalere_census.warm_cache import (
        bulk_parquet_path,
        resolve_bulk_cache_dir,
        vintage_tag_5yr,
    )

    cache_root = resolve_bulk_cache_dir()
    vintage_tag = vintage_tag_5yr(int(vintage))

    # Group requested variables by table prefix (everything before the first '_').
    tables_to_vars: dict[str, list[str]] = {}
    for v in requested_vars:
        if "_" not in v:
            return None  # non-standard var, can't bulk-read
        table = v.split("_", 1)[0]
        tables_to_vars.setdefault(table, []).append(v)

    table_paths: dict[str, Path] = {}
    for table in tables_to_vars:
        p = bulk_parquet_path(cache_root, "tract", vintage_tag, table, state_fips)
        if not p.exists():
            return None
        table_paths[table] = p

    geo_cols = ["GEOID", "state", "county", "tract"]
    merged: Optional[pd.DataFrame] = None
    for table, var_list in tables_to_vars.items():
        df = pd.read_parquet(table_paths[table])
        keep = [c for c in geo_cols if c in df.columns]
        for v in var_list:
            if v not in df.columns:
                return None  # missing requested variable inside the parquet
            keep.append(v)
            if include_moe:
                moe = v[:-1] + "M" if v.endswith("E") else None
                if moe and moe in df.columns:
                    keep.append(moe)
        slice_df = df[keep].copy()

        if merged is None:
            merged = slice_df
            continue
        non_geo_cols = [c for c in slice_df.columns if c not in geo_cols]
        merged = merged.merge(slice_df[["GEOID"] + non_geo_cols], on="GEOID", how="inner")

    return merged
