"""
Census ACS PUMS fetch: split H (housing unit) and P (person) record fetches,
joined on SERIALNO, with PUMA-county weight adjustment for shared PUMAs.

Usage:
    from telltalere_census.pums_fetch import fetch_pums_data
    from telltalere_census.puma_crosswalk import get_pumas_for_county
    from telltalere_census.county import resolve_county

    state, county, full, name = resolve_county(fips="18097")
    pumas = get_pumas_for_county(state, county)
    df = fetch_pums_data(state, pumas, acs_type="acs5", acs_year=2024)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from telltalere_census.cache import (
    pums_h_filename,
    pums_p_filename,
    resolve_cache_dir,
)
from telltalere_census.variables import (
    PUMS_DEFAULT_HOUSING_VARS,
    PUMS_DEFAULT_PERSON_VARS,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_puma_var(acs_type: str, acs_year: int) -> str:
    """Which PUMA field name the given ACS vintage uses."""
    # 2024 ACS5 and all ACS1 use PUMA. Only older ACS5 (<=2022) used PUMA20.
    if acs_type == "acs5" and acs_year <= 2022:
        return "PUMA20"
    return "PUMA"


def _fetch_records(
    var_list: list[str],
    state_fips: str,
    puma_codes: list[str],
    acs_type: str,
    acs_year: int,
    puma_var: str,
    label: str,
) -> list[dict]:
    """
    Generic PUMS fetch: pull `var_list` for given state/PUMAs.
    Returns a list of dicts (one per record row).
    """
    vars_to_fetch = list(var_list)
    if puma_var not in vars_to_fetch:
        vars_to_fetch.append(puma_var)

    base_url = f"https://api.census.gov/data/{acs_year}/acs/{acs_type}/pums"
    var_str = ",".join(vars_to_fetch)
    all_records = []

    # Fetch entire state at once (faster for batch runs, enables full-state caching)
    params = {"get": var_str, "for": f"state:{state_fips}"}
    census_key = os.environ.get("CENSUS_API_KEY", "")
    if census_key:
        params["key"] = census_key
    resp = requests.get(base_url, params=params, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    headers = data[0]
    for row in data[1:]:
        all_records.append(dict(zip(headers, row)))

    return all_records


def _fetch_h_records(
    state_fips: str,
    puma_codes: list[str],
    acs_year: int,
    acs_type: str,
    housing_vars: list[str],
    cache_dir: Optional[Path],
) -> pd.DataFrame:
    """
    Fetch housing unit (H) records. One row per housing unit.
    Cached as pums_{acs_type}_{acs_year}_{state_fips}_H.parquet in the
    resolved cache dir.
    """
    puma_var = _build_puma_var(acs_type, acs_year)
    cache_dir = resolve_cache_dir(cache_dir)
    cache_path = cache_dir / pums_h_filename(acs_type, acs_year, state_fips)

    if cache_path.exists():
        print(f"  Loading cached H records from {cache_path.name}...")
        df = pd.read_parquet(cache_path)
        puma_col = "PUMA" if "PUMA" in df.columns else "PUMA20"
        df = df[df[puma_col].astype(str).isin(set(puma_codes))].copy()
        if puma_col == "PUMA20":
            df = df.rename(columns={"PUMA20": "PUMA"})
        return df

    print(f"  Fetching {acs_type.upper()} {acs_year} H records for state {state_fips}...")
    records = _fetch_records(housing_vars, state_fips, puma_codes,
                             acs_type, acs_year, puma_var, "H")
    if not records:
        sys.exit("No H records retrieved. Check PUMA codes and ACS year.")

    df = pd.DataFrame(records)

    # Normalize PUMA column name
    if "PUMA20" in df.columns and "PUMA" not in df.columns:
        df = df.rename(columns={"PUMA20": "PUMA"})

    # Census API returns person-level rows even for H-only variable requests
    # (H values replicated to each person in the unit). Deduplicate to one
    # row per housing unit by taking the first row per SERIALNO.
    before = len(df)
    df = df.drop_duplicates(subset=["SERIALNO"], keep="first")
    print(f"  Deduped {before:,} person-rows to {len(df):,} housing units")

    # All Census API values come back as strings. Convert numeric columns.
    int_cols = ["NP", "TEN", "WGTP", "BLD", "YRBLT", "BDSP", "RMSP",
                "VEH", "HHT", "HUPAC", "NOC", "NPF", "R18", "R65",
                "WIF", "MULTG", "LNGI", "MV", "FS", "BROADBND", "HISPEED"]
    for col in int_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    float_cols = ["HINCP", "VALP", "GRNTP", "RNTP"]
    for col in float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # POVPIP: can be 'b' for N/A (group quarters/institutionalized).
    if "POVPIP" in df.columns:
        df["POVPIP"] = pd.to_numeric(df["POVPIP"], errors="coerce").astype("Int64")

    print(f"  {len(df):,} H records cached (full state)")
    df.to_parquet(cache_path, index=False)
    df = df[df["PUMA"].astype(str).isin(set(puma_codes))].copy()
    return df


def _fetch_p_records(
    state_fips: str,
    puma_codes: list[str],
    acs_year: int,
    acs_type: str,
    person_vars: list[str],
    cache_dir: Optional[Path],
) -> pd.DataFrame:
    """
    Fetch person (P) records, filtered to householders (RELSHIPP == '20').
    One row per householder (i.e., one per occupied housing unit).
    Cached as pums_{acs_type}_{acs_year}_{state_fips}_P.parquet in the
    resolved cache dir.
    """
    puma_var = _build_puma_var(acs_type, acs_year)
    cache_dir = resolve_cache_dir(cache_dir)
    cache_path = cache_dir / pums_p_filename(acs_type, acs_year, state_fips)

    if cache_path.exists():
        print(f"  Loading cached P records from {cache_path.name}...")
        df = pd.read_parquet(cache_path)
        puma_col = "PUMA" if "PUMA" in df.columns else "PUMA20"
        df = df[df[puma_col].astype(str).isin(set(puma_codes))].copy()
        if puma_col == "PUMA20":
            df = df.rename(columns={"PUMA20": "PUMA"})
        return df

    print(f"  Fetching {acs_type.upper()} {acs_year} P records for state {state_fips}...")
    records = _fetch_records(person_vars, state_fips, puma_codes,
                             acs_type, acs_year, puma_var, "P")
    if not records:
        sys.exit("No P records retrieved. Check PUMA codes and ACS year.")

    df = pd.DataFrame(records)

    if "PUMA20" in df.columns and "PUMA" not in df.columns:
        df = df.rename(columns={"PUMA20": "PUMA"})

    # Filter to householders only (RELSHIPP comes back as string '20')
    df = df[df["RELSHIPP"] == "20"].copy()

    int_cols = ["AGEP", "SEX", "MIG", "SCHG", "SCHL", "ESR", "COW",
                "DIS", "HICOV", "CIT", "LANX"]
    for col in int_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    float_cols = ["WAGP", "PINCP", "SSIP"]
    for col in float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"  {len(df):,} householder P records cached (full state)")
    df.to_parquet(cache_path, index=False)
    df = df[df["PUMA"].astype(str).isin(set(puma_codes))].copy()
    return df


def _apply_puma_weights(
    df: pd.DataFrame, county_fips: str, weights_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Adjust WGTP for each record based on the county's share of its PUMA's
    population. For fully county-contained PUMAs (weight=1.0): no change.
    For shared PUMAs: WGTP *= county_weight_for_this_puma.
    """
    state_fips = county_fips[:2]
    county_w = weights_df[
        (weights_df["state_fips"] == state_fips) &
        (weights_df["county_fips"] == county_fips)
    ][["puma_code", "weight"]]

    if county_w.empty:
        return df

    if (county_w["weight"] == 1.0).all():
        return df

    df = df.copy()
    df["_puma_str"] = df["PUMA"].astype(str).str.zfill(5)
    df = df.merge(county_w, left_on="_puma_str",
                  right_on="puma_code", how="left")
    df["weight"] = df["weight"].fillna(1.0)
    # Keep WGTP as float to avoid rounding small weights to 0
    # (Int64 rounding would lose records with small PUMA shares)
    df["WGTP"] = df["WGTP"].astype(float) * df["weight"]
    df = df.drop(columns=["_puma_str", "puma_code", "weight"])
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_pums_data(
    state_fips: str,
    puma_codes: list[str],
    acs_type: str = "acs5",
    acs_year: int = 2024,
    housing_vars: Optional[list[str]] = None,
    person_vars: Optional[list[str]] = None,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Fetch PUMS housing + person records, filter to householders, join on
    SERIALNO, and apply PUMA-county population weights for shared PUMAs.

    Args:
        state_fips: 2-digit state FIPS code
        puma_codes: list of 5-digit PUMA codes to include
        acs_type: "acs1" or "acs5"
        acs_year: ACS vintage year
        housing_vars: override for PUMS_DEFAULT_HOUSING_VARS
        person_vars: override for PUMS_DEFAULT_PERSON_VARS
        cache_dir: override for parquet cache location

    Returns:
        DataFrame with one row per occupied housing unit, joined
        householder demographics, typed numeric columns, and WGTP adjusted
        to the county (when the caller previously invoked
        `puma_crosswalk.get_pumas_for_county`).
    """
    h_vars = housing_vars or PUMS_DEFAULT_HOUSING_VARS
    p_vars = person_vars or PUMS_DEFAULT_PERSON_VARS

    h_df = _fetch_h_records(state_fips, puma_codes, acs_year, acs_type, h_vars, cache_dir)
    p_df = _fetch_p_records(state_fips, puma_codes, acs_year, acs_type, p_vars, cache_dir)

    # Drop RELSHIPP from P before join (no longer needed after filtering)
    # Drop PUMA from P to avoid _x/_y suffix collision (PUMA is on H already)
    p_cols_to_drop = [c for c in ["RELSHIPP", "PUMA", "state"] if c in p_df.columns]
    p_df = p_df.drop(columns=p_cols_to_drop)

    # Left join: keep all H records, attach householder P data where available
    df = h_df.merge(p_df, on="SERIALNO", how="left")

    # Filter to occupied units only (NP > 0) with valid income and weight
    df = df.dropna(subset=["HINCP", "NP", "TEN", "WGTP"])
    df = df[df["NP"] > 0].copy()

    # Apply PUMA-county population weights for shared PUMAs
    from telltalere_census.puma_crosswalk import (
        _last_county_fips as _module_last,
        build_puma_county_weights,
    )
    # _last_county_fips is module-level in telltalere_census.puma_crosswalk;
    # read it fresh each call.
    from telltalere_census import puma_crosswalk as _pxw
    county_fips = _pxw._last_county_fips
    if county_fips and county_fips[:2] == state_fips:
        weights_df = build_puma_county_weights()
        df = _apply_puma_weights(df, county_fips, weights_df)

    print(f"  Joined: {len(df):,} occupied housing units with householder data")
    if "AGEP" in df.columns:
        print(f"    AGEP coverage: {df['AGEP'].notna().sum():,}/{len(df):,}")
    if "MIG" in df.columns:
        print(f"    MIG coverage: {df['MIG'].notna().sum():,}/{len(df):,}")
    return df
