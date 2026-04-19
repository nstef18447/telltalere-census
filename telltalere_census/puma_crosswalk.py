"""
PUMA-to-county crosswalk: download and parse Census tract-to-PUMA file,
plus population-weighted allocation for multi-county PUMAs.

On-disk state:
  - Tract-to-PUMA crosswalk: fetched from Census, cached as CSV in the
    configured cache dir (`tract_to_puma_2020.csv`).
  - Tract population counts: fetched from the 2020 Decennial PL API, cached
    per-state as parquet (`tract_pop_2020_{state_fips}.parquet`).
  - Per-PUMA/county population weights: shipped with the package at
    `telltalere_census/data/puma_county_weights.parquet`. If that file is
    missing (e.g. a clean dev checkout), the table is rebuilt from tract
    populations on demand and cached in the configured cache dir.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from telltalere_census.cache import (
    package_data_path,
    resolve_cache_dir,
    tract_pop_filename,
)
from telltalere_census.variables import CROSSWALK_URL


# Module-level state: last county FIPS looked up by get_pumas_for_county.
# Used by pums_fetch.fetch_pums_data() to apply PUMA-county weights
# without requiring a change to the caller's signature.
#
# FOLLOW-UP: This global is a thread-safety and reentrancy hazard. Preserved
# for behavior compatibility with ami-tool; flagged in REFACTOR_NOTES.md as
# a priority follow-up before any downstream product processes counties in
# parallel.
_last_county_fips: Optional[str] = None


def _load_crosswalk(cache_dir: Optional[Path] = None):
    """Load and normalize the tract-to-PUMA crosswalk file."""
    cache_dir = resolve_cache_dir(cache_dir)
    cache_path = cache_dir / "tract_to_puma_2020.csv"
    if not cache_path.exists():
        print("  Downloading PUMA-to-county crosswalk...")
        resp = requests.get(CROSSWALK_URL)
        resp.raise_for_status()
        with open(cache_path, "wb") as f:
            f.write(resp.content)

    df = pd.read_csv(cache_path, dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]

    st_col = [c for c in df.columns if "STATE" in c and "FP" in c][0]
    co_col = [c for c in df.columns if "COUNTY" in c and "FP" in c][0]
    puma_col = [c for c in df.columns if "PUMA" in c][0]
    tract_col = [c for c in df.columns if "TRACT" in c][0]

    return df, st_col, co_col, puma_col, tract_col


def get_pumas_for_county(
    state_fips: str, county_fips: str, cache_dir: Optional[Path] = None
) -> list[str]:
    """
    Find all PUMAs that overlap the given county.

    Downloads and caches the Census tract-to-PUMA crosswalk on first call.
    Stores the county FIPS in module-level state for `fetch_pums_data`'s
    weight-application step.

    Args:
        state_fips: 2-digit state FIPS code (e.g. "18")
        county_fips: 3-digit county FIPS code (e.g. "097")
        cache_dir: optional override for the parquet/csv cache directory

    Returns:
        List of 5-digit PUMA codes as strings.
    """
    global _last_county_fips
    _last_county_fips = str(state_fips).zfill(2) + str(county_fips).zfill(3)

    df, st_col, co_col, puma_col, _ = _load_crosswalk(cache_dir)

    mask = (df[st_col] == state_fips) & (df[co_col] == county_fips)
    pumas = df.loc[mask, puma_col].unique().tolist()

    if not pumas:
        sys.exit(f"No PUMAs found for county {state_fips}{county_fips}")

    print(f"  Found {len(pumas)} PUMA(s) for county: {pumas}")
    return pumas


# ---------------------------------------------------------------------------
# PUMA-county population weights
# ---------------------------------------------------------------------------

def _fetch_tract_populations(
    state_fips: str, cache_dir: Optional[Path] = None
) -> pd.DataFrame:
    """
    Fetch tract-level total population from 2020 Census PL data.
    Caches per-state as parquet. Returns DataFrame with columns:
        state (str), county (str), tract (str), population (int)
    """
    cache_dir = resolve_cache_dir(cache_dir)
    cache_path = cache_dir / tract_pop_filename(state_fips)
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    url = ("https://api.census.gov/data/2020/dec/pl"
           f"?get=P1_001N&for=tract:*&in=state:{state_fips}")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    headers = data[0]
    rows = data[1:]
    df = pd.DataFrame(rows, columns=headers)
    df = df.rename(columns={"P1_001N": "population"})
    df["population"] = pd.to_numeric(df["population"], errors="coerce").fillna(0).astype(int)
    df.to_parquet(cache_path, index=False)
    return df


def build_puma_county_weights(
    cache_path: Optional[Path] = None, cache_dir: Optional[Path] = None
) -> pd.DataFrame:
    """
    Compute the population-weighted share of each PUMA that belongs to each
    county.

    Returns a DataFrame with columns:
        state_fips  (str, 2-digit)
        puma_code   (str, 5-digit)
        county_fips (str, 5-digit)
        weight      (float, 0.0 to 1.0)

    For PUMAs entirely within one county: weight = 1.0
    For PUMAs spanning multiple counties: weights sum to 1.0

    Resolution order for the precomputed table:
      1. explicit `cache_path` argument
      2. shipped package data at telltalere_census/data/puma_county_weights.parquet
      3. rebuild from tract populations and cache to resolve_cache_dir()

    Args:
        cache_path: optional explicit path to an existing weights parquet
        cache_dir: optional override for where a freshly-built table is cached
    """
    if cache_path is not None:
        cache_path = Path(cache_path)
        if cache_path.exists():
            return pd.read_parquet(cache_path)
    else:
        shipped = package_data_path("puma_county_weights.parquet")
        if shipped.exists():
            return pd.read_parquet(shipped)
        cache_path = resolve_cache_dir(cache_dir) / "puma_county_weights.parquet"
        if cache_path.exists():
            return pd.read_parquet(cache_path)

    print("  Building PUMA-county allocation weights...")
    xwalk, st_col, co_col, puma_col, tract_col = _load_crosswalk(cache_dir)

    states = sorted(xwalk[st_col].unique())
    print(f"    Fetching tract populations for {len(states)} states...")

    all_pop = []
    for i, st in enumerate(states):
        if i > 0 and i % 10 == 0:
            print(f"    ... {i}/{len(states)} states fetched")
        try:
            pop_df = _fetch_tract_populations(st, cache_dir)
            all_pop.append(pop_df)
        except Exception as e:
            print(f"    WARNING: Failed to fetch tract pop for state {st}: {e}")

    pop = pd.concat(all_pop, ignore_index=True)

    pop["state"] = pop["state"].astype(str).str.zfill(2)
    pop["county"] = pop["county"].astype(str).str.zfill(3)
    pop["tract"] = pop["tract"].astype(str).str.zfill(6)

    xwalk = xwalk.copy()
    xwalk["_st"] = xwalk[st_col].astype(str).str.zfill(2)
    xwalk["_co"] = xwalk[co_col].astype(str).str.zfill(3)
    xwalk["_tract"] = xwalk[tract_col].astype(str).str.zfill(6)
    xwalk["_puma"] = xwalk[puma_col].astype(str).str.zfill(5)

    merged = xwalk.merge(
        pop, left_on=["_st", "_co", "_tract"],
        right_on=["state", "county", "tract"], how="left"
    )
    merged["population"] = merged["population"].fillna(0).astype(int)

    grouped = (merged.groupby(["_st", "_puma", "_co"])["population"]
               .sum().reset_index())
    grouped.columns = ["state_fips", "puma_code", "county_3", "county_pop"]

    puma_totals = (grouped.groupby(["state_fips", "puma_code"])["county_pop"]
                   .sum().reset_index())
    puma_totals.columns = ["state_fips", "puma_code", "puma_total_pop"]

    weights = grouped.merge(puma_totals, on=["state_fips", "puma_code"])
    weights["weight"] = weights.apply(
        lambda r: r["county_pop"] / r["puma_total_pop"]
        if r["puma_total_pop"] > 0 else 0.0, axis=1
    )
    weights["county_fips"] = weights["state_fips"] + weights["county_3"]

    result = weights[["state_fips", "puma_code", "county_fips", "weight"]].copy()

    puma_sums = result.groupby(["state_fips", "puma_code"])["weight"].sum()
    bad = puma_sums[(puma_sums < 0.99) | (puma_sums > 1.01)]
    if len(bad) > 0:
        print(f"    WARNING: {len(bad)} PUMAs have weight sums outside [0.99, 1.01]")

    result.to_parquet(cache_path, index=False)
    n_shared = (result.groupby(["state_fips", "puma_code"]).size() > 1).sum()
    print(f"    {len(result):,} county-PUMA pairs, "
          f"{n_shared:,} shared PUMAs across US")
    return result
