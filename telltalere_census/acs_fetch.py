"""
Census ACS 5-year summary-table fetch at block-group or tract geography.

Parallel structure to `pums_fetch` but for aggregated ACS tables at
sub-county geographies. Variable-list driven — the caller passes the list
of variable codes to retrieve.

Endpoint:
    https://api.census.gov/data/{vintage}/acs/{acs_type}
        ?get=<vars>
        &for=block group:*        (or "tract:*")
        &in=state:{st} county:*

API `in=` clause differs by geography (verified against 2024 ACS5):
  block group:*  requires `in=state:{st} county:*` — state-only returns
                 400 "unknown/unsupported geography hierarchy".
  tract:*        accepts `in=state:{st}` directly (county wildcard also
                 works but is redundant).

Both patterns return the entire state in a single API call, which keeps
caching simple (one parquet per state/vintage/geography).

MOE pairing: when `include_moe=True`, every `_E` estimate variable is
automatically paired with its `_M` margin-of-error counterpart (callers
never need to type MOEs). The pairing is internal — the returned
DataFrame contains both `_E` and `_M` columns.

Variable-limit chunking: the Census API rejects `get=` lists exceeding
50 variables. `fetch_acs_data` sorts its variable list, splits into
deterministic chunks of 50, fetches each chunk independently, and
inner-joins on the composite GEOID. A non-zero row-count delta across
chunks is logged as a warning and the offending rows are dropped.

Special-value normalization: Census uses sentinel integers
(-555555555, -222222222, -333333333, -666666666, -888888888,
-999999999) and sentinel strings ("", "*", "**", "***", "(X)", "null",
"N") to indicate suppression, unreliable estimates, or not-applicable.
All sentinels are mapped to pandas NA; numeric columns use nullable
`Int64` (integer values) or `Float64` (fractional values, e.g. average
household size) so downstream `.isna()` checks work uniformly.

Caching: parquet at `acs5_{vintage}_{state_fips}_{geography_tag}.parquet`
in the resolved cache dir. One file per `(vintage, state, geography)`
tuple. A cache that is missing any requested variable triggers a fresh
full-state fetch of the UNION of cached+requested variables, written
back as one coherent snapshot. Partial-delta merging is deliberately
avoided — all cells in a given parquet reflect the same API call.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import requests

from telltalere_census.cache import (
    bg_acs5_filename,
    resolve_cache_dir,
)
from telltalere_census.variables import ACS_BG_DEFAULT_VARS


logger = logging.getLogger(__name__)


_SENTINEL_INTS = {
    -555555555, -222222222, -333333333, -666666666, -888888888, -999999999,
}
_SENTINEL_STRINGS = {"", "*", "**", "***", "(X)", "null", "N", "-"}

# Census ACS5 requires different `in=` specificity by geography. Tested
# against 2024 ACS5 on 2026-04-19:
#   block group:*  requires `in=state:{st} county:*`
#   tract:*        accepts `in=state:{st}`
_API_IN_CLAUSE_BG = "state:{st} county:*"
_API_IN_CLAUSE_TRACT = "state:{st}"
_MAX_VARS_PER_CALL = 50


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_acs_data(
    state_fips: str,
    variables: Optional[list[str]] = None,
    geography: Literal["block group", "tract"] = "block group",
    vintage: int = 2024,
    acs_type: Literal["acs5", "acs1"] = "acs5",
    include_moe: bool = True,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Fetch ACS summary-table data for all block groups or tracts in a state.

    Args:
        state_fips: 2-digit state FIPS code.
        variables: list of estimate variable codes (e.g. "B25003_001E").
            If None, uses ACS_BG_DEFAULT_VARS.
        geography: "block group" (12-char GEOID) or "tract" (11-char GEOID).
        vintage: ACS vintage year.
        acs_type: "acs5" (default) or "acs1".
        include_moe: if True, auto-fetch `_M` companion for every `_E`
            estimate variable. The MOE vars appear as `_M` columns in the
            returned DataFrame.
        cache_dir: override parquet cache location. Defaults to resolved
            cache dir (see cache.resolve_cache_dir).

    Returns:
        DataFrame with one row per geography. Columns:
          - GEOID: composite geography key (12 chars for BG, 11 for tract)
          - state, county, tract, block_group (block_group omitted for tract)
          - one column per requested estimate variable (nullable Int64/Float64)
          - one column per MOE companion if include_moe=True (suffixed `_M`)

    Raises:
        ValueError: on unsupported geography.
        RuntimeError: if the API returns no data and no cache exists.
    """
    if geography not in ("block group", "tract"):
        raise ValueError(f"geography must be 'block group' or 'tract', got {geography!r}")

    est_vars = list(variables) if variables is not None else list(ACS_BG_DEFAULT_VARS)
    if not est_vars:
        raise ValueError("variables list is empty")

    # Auto-pair MOEs
    requested = list(est_vars)
    if include_moe:
        for v in est_vars:
            moe = _moe_of(v)
            if moe is not None and moe not in requested:
                requested.append(moe)

    cache_path = _resolve_cache_path(
        resolve_cache_dir(cache_dir), vintage, state_fips, geography
    )

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        missing = [v for v in requested if v not in cached.columns]
        if not missing:
            logger.info(
                "Loading cached %s records from %s", geography, cache_path.name
            )
            return cached.copy()

        logger.info(
            "Cache %s missing %d vars %s; re-fetching union of cached + requested.",
            cache_path.name, len(missing), missing,
        )
        # Union of cached data vars and newly requested vars
        existing_vars = _extract_data_vars(cached)
        fetch_union = sorted(set(existing_vars) | set(requested))
    else:
        fetch_union = sorted(set(requested))

    logger.info(
        "Fetching %s %s for state %s (%d vars, %d chunk(s))...",
        acs_type.upper(), vintage, state_fips, len(fetch_union),
        _n_chunks(len(fetch_union)),
    )

    df = _fetch_full_state(
        state_fips=state_fips,
        variables=fetch_union,
        geography=geography,
        vintage=vintage,
        acs_type=acs_type,
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    logger.info(
        "Wrote %d %s rows to %s", len(df), geography, cache_path.name
    )

    return df.copy()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _resolve_cache_path(
    cache_dir: Path, vintage: int, state_fips: str,
    geography: str,
) -> Path:
    if geography == "block group":
        fname = bg_acs5_filename(vintage, state_fips)
    else:
        # parallel convention: acs5_{year}_{state}_tract.parquet
        fname = f"acs5_{vintage}_{state_fips}_tract.parquet"
    return cache_dir / fname


def _extract_data_vars(df: pd.DataFrame) -> list[str]:
    """Return the ACS variable columns from a cached frame (exclude geography)."""
    geography_cols = {"GEOID", "state", "county", "tract", "block_group"}
    return [c for c in df.columns if c not in geography_cols]


# ---------------------------------------------------------------------------
# API fetch + chunking
# ---------------------------------------------------------------------------

def _n_chunks(n_vars: int) -> int:
    return (n_vars + _MAX_VARS_PER_CALL - 1) // _MAX_VARS_PER_CALL


def _chunk_vars(variables: list[str]) -> list[list[str]]:
    """Deterministic chunking: sort, then split into _MAX_VARS_PER_CALL groups."""
    sorted_vars = sorted(set(variables))
    return [
        sorted_vars[i:i + _MAX_VARS_PER_CALL]
        for i in range(0, len(sorted_vars), _MAX_VARS_PER_CALL)
    ]


def _fetch_full_state(
    state_fips: str,
    variables: list[str],
    geography: str,
    vintage: int,
    acs_type: str,
) -> pd.DataFrame:
    """Chunked fetch + inner-join on composite GEOID. Handles typing and GEOID."""
    chunks = _chunk_vars(variables)

    merged: Optional[pd.DataFrame] = None
    for i, chunk in enumerate(chunks, 1):
        logger.info("  chunk %d/%d (%d vars)", i, len(chunks), len(chunk))
        raw = _fetch_one_chunk(
            state_fips=state_fips,
            chunk_vars=chunk,
            geography=geography,
            vintage=vintage,
            acs_type=acs_type,
        )
        chunk_df = _build_chunk_df(raw, chunk, geography)

        if merged is None:
            merged = chunk_df
            continue

        before = len(merged)
        merged = merged.merge(
            chunk_df.drop(columns=[
                c for c in ("state", "county", "tract", "block_group")
                if c in chunk_df.columns
            ]),
            on="GEOID",
            how="inner",
        )
        after = len(merged)
        if after != before or after != len(chunk_df):
            logger.warning(
                "Chunk %d/%d geography mismatch: prior=%d, chunk=%d, merged=%d (dropped %d)",
                i, len(chunks), before, len(chunk_df), after, max(before, len(chunk_df)) - after,
            )

    assert merged is not None, "no chunks fetched"
    return merged


def _fetch_one_chunk(
    state_fips: str,
    chunk_vars: list[str],
    geography: str,
    vintage: int,
    acs_type: str,
) -> list[list]:
    """Hit the Census API once. Returns raw JSON as list-of-lists (header + rows)."""
    base_url = f"https://api.census.gov/data/{vintage}/acs/{acs_type}"
    in_clause = (_API_IN_CLAUSE_BG if geography == "block group" else _API_IN_CLAUSE_TRACT).format(st=state_fips)
    params = {
        "get": ",".join(chunk_vars),
        "for": f"{geography}:*",
        "in": in_clause,
    }
    census_key = os.environ.get("CENSUS_API_KEY", "")
    if census_key:
        params["key"] = census_key

    resp = requests.get(base_url, params=params, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    if not data or len(data) < 2:
        raise RuntimeError(
            f"No {acs_type.upper()} {vintage} {geography} records returned for "
            f"state {state_fips} (chunk of {len(chunk_vars)} vars). "
            "Check variable codes and vintage."
        )
    return data


def _build_chunk_df(
    raw: list[list], chunk_vars: list[str], geography: str,
) -> pd.DataFrame:
    """Parse JSON rows, normalize sentinels, type columns, build GEOID."""
    headers = raw[0]
    rows = raw[1:]
    df = pd.DataFrame(rows, columns=headers)

    # Sentinel -> NA, then dtype inference per variable
    for v in chunk_vars:
        if v not in df.columns:
            raise RuntimeError(
                f"API response missing requested variable {v!r}. "
                f"Returned columns: {list(df.columns)}"
            )
        df[v] = _normalize_numeric_column(df[v])

    # Geography atoms — rename "block group" (with space) for downstream ergonomics
    if geography == "block group":
        if "block group" in df.columns:
            df = df.rename(columns={"block group": "block_group"})
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["county"].astype(str).str.zfill(3)
            + df["tract"].astype(str).str.zfill(6)
            + df["block_group"].astype(str).str.zfill(1)
        )
    else:
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["county"].astype(str).str.zfill(3)
            + df["tract"].astype(str).str.zfill(6)
        )

    return df


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------

def _normalize_numeric_column(s: pd.Series) -> pd.Series:
    """
    Map Census sentinel values to pandas NA; coerce to nullable Int64 if all
    values are whole numbers, else nullable Float64.
    """
    # All API values arrive as strings; strip whitespace first
    stripped = s.astype(str).str.strip()

    # Replace string sentinels with NA before numeric coercion
    stripped = stripped.where(~stripped.isin(_SENTINEL_STRINGS), other=None)

    # Coerce to float first (handles fractional and negative sentinels uniformly)
    numeric = pd.to_numeric(stripped, errors="coerce")

    # Replace integer sentinels with NA
    sentinel_mask = numeric.isin(_SENTINEL_INTS)
    numeric = numeric.where(~sentinel_mask, other=float("nan"))

    # If every non-NA value is an integer, store as nullable Int64; else Float64
    nonna = numeric.dropna()
    if len(nonna) > 0 and (nonna % 1 == 0).all():
        return numeric.astype("Int64")
    return numeric.astype("Float64")


def _moe_of(var: str) -> Optional[str]:
    """Return the MOE companion name for an estimate variable.

    ACS convention: estimates end in `E`, MOEs end in `M` (same stem).
    Returns None for codes not ending in `E` (e.g. non-standard variables
    like `NAME`, `GEO_ID`).
    """
    if var.endswith("E"):
        return var[:-1] + "M"
    return None
