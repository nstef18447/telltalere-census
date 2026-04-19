"""
Census ACS 5-year block-group summary fetch.

Parallel structure to `pums_fetch` but for aggregated ACS5 tables at the
block-group geography. Variable-list driven — the caller passes the list of
variable codes to retrieve.

Endpoint:  https://api.census.gov/data/{year}/acs/acs5
            ?get=...&for=block%20group:*&in=state:{st}%20county:{co}

Block-group GEOID is constructed from STATE + COUNTY + TRACT + BLOCK GROUP.

MOE (margin of error) variables have the same code with an `M` suffix
(e.g. `B19013_001E` has MOE counterpart `B19013_001M`). Pass
`include_moe=True` to fetch them as additional columns.

Cache: parquet at `acs5_{year}_{state_fips}_bg.parquet` in the resolved
cache dir. A single state-level cache serves all counties within the state —
caching is keyed on `(year, state)` not `(year, state, var_list)`, so callers
requesting different variables WILL cause cache misses. A separate cache file
per var-list hash is deferred — flagged in REFACTOR_NOTES.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from telltalere_census.cache import bg_acs5_filename, resolve_cache_dir
from telltalere_census.variables import ACS_BG_DEFAULT_VARS


def _moe_of(var: str) -> Optional[str]:
    """Return the MOE variable name for an estimate variable, or None.
    ACS convention: estimate suffix `E`, MOE suffix `M`.
    """
    if var.endswith("E"):
        return var[:-1] + "M"
    return None


def fetch_bg_data(
    state_fips: str,
    county_fips: Optional[str] = None,
    variables: Optional[list[str]] = None,
    year: int = 2024,
    include_moe: bool = False,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Fetch ACS5 block-group summary records for a state (optionally filtered
    to a county).

    Args:
        state_fips: 2-digit state FIPS
        county_fips: optional 3-digit county FIPS to filter to; if omitted,
            returns all block groups in the state
        variables: list of variable codes (e.g. ["B01003_001E", "B19013_001E"]).
            If None, uses ACS_BG_DEFAULT_VARS.
        year: ACS5 vintage year
        include_moe: if True, also fetch the `_M` MOE counterpart for every
            estimate variable (those ending in "E")
        cache_dir: override for parquet cache location

    Returns:
        DataFrame indexed by GEOID (12-digit block group) with columns for
        each requested variable plus `state`, `county`, `tract`,
        `block_group`, and `GEOID`.
    """
    est_vars = list(variables) if variables is not None else list(ACS_BG_DEFAULT_VARS)

    fetch_vars = list(est_vars)
    if include_moe:
        for v in est_vars:
            moe = _moe_of(v)
            if moe is not None and moe not in fetch_vars:
                fetch_vars.append(moe)

    cache_dir = resolve_cache_dir(cache_dir)
    cache_path = cache_dir / bg_acs5_filename(year, state_fips)

    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        missing = [v for v in fetch_vars if v not in df.columns]
        if not missing:
            print(f"  Loading cached block-group records from {cache_path.name}...")
            return _filter_bg_df(df, county_fips)
        print(f"  Cached {cache_path.name} missing vars {missing}; re-fetching full state.")

    print(f"  Fetching ACS5 {year} block-group data for state {state_fips}...")
    df = _fetch_bg_from_api(state_fips, fetch_vars, year)

    # Type coercion: all API values come back as strings.
    for v in fetch_vars:
        df[v] = pd.to_numeric(df[v], errors="coerce")

    # Build GEOID = state(2) + county(3) + tract(6) + block group(1)
    df["GEOID"] = (
        df["state"].astype(str).str.zfill(2)
        + df["county"].astype(str).str.zfill(3)
        + df["tract"].astype(str).str.zfill(6)
        + df["block group"].astype(str).str.zfill(1)
    )

    # Stable column rename to avoid the awkward space in "block group"
    df = df.rename(columns={"block group": "block_group"})

    df.to_parquet(cache_path, index=False)
    print(f"  {len(df):,} block groups cached (full state)")

    return _filter_bg_df(df, county_fips)


def _fetch_bg_from_api(state_fips: str, var_list: list[str], year: int) -> pd.DataFrame:
    """Hit the ACS5 API once at state level and return a DataFrame."""
    base_url = f"https://api.census.gov/data/{year}/acs/acs5"
    params = {
        "get": ",".join(var_list),
        "for": "block group:*",
        "in": f"state:{state_fips}",
    }
    census_key = os.environ.get("CENSUS_API_KEY", "")
    if census_key:
        params["key"] = census_key

    resp = requests.get(base_url, params=params, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if not data or len(data) < 2:
        sys.exit(
            f"No ACS5 {year} block-group records returned for state {state_fips}. "
            "Check variable list and vintage."
        )
    headers = data[0]
    return pd.DataFrame(data[1:], columns=headers)


def _filter_bg_df(df: pd.DataFrame, county_fips: Optional[str]) -> pd.DataFrame:
    if county_fips is None:
        return df.copy()
    co = str(county_fips).zfill(3)
    return df[df["county"].astype(str).str.zfill(3) == co].copy()
