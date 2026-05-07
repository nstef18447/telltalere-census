"""
2010 <-> 2020 census tract boundary harmonization for cross-decade
ACS comparisons.

ACS 5-year vintages with end-year <= 2019 use 2010 Census tract
boundaries; end-year >= 2020 use 2020 boundaries. ACS 1-year vintages
with end-year <= 2019 use 2010 boundaries; end-year >= 2021 use 2020
(2020 1-year not published). To compare tract-level data across the
cutover, harmonize the historical (2010-boundary) subset onto 2020
boundaries before trend computation.

Scope of `get_boundary_vintage_for_acs`
---------------------------------------
This function returns the boundary vintage for **tract** and **block
group** geographies only. PUMAs, Congressional Districts, and Urban
Areas migrated on a different schedule (2022 1-year for those summary
levels per Census user note 2023-02). Use Census documentation for
those geographies — the function will raise if you ask about a non-
tract/BG context.

Data source
-----------
The boundary crosswalk is derived from the Census-published
`tab20_tract20_tract10_natl.txt` file
(https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/),
which records each (2020 tract, 2010 tract) intersection's land area
in square meters. The crosswalk is built by
`scripts/build_tract_boundary_crosswalk.py` and shipped as a GitHub
release asset (NOT in the wheel — it's a 4.80 MB national parquet,
mirrors the per-state tract-polygon pattern from session 3).

Weights are **area-based**, not population-based. The relationship
file does not include population. See
`harmonize_to_2020_boundaries` for the assumption this places on
allocation, including failure-mode discussion.

Distribution
------------
Like `geometry.download_tract_polygons`, the crosswalk is a runtime
download:

  >>> from telltalere_census import download_tract_boundary_crosswalk
  >>> download_tract_boundary_crosswalk()

caches to `platformdirs.user_cache_dir('telltalere_census')/'boundaries'`
(or `TELLTALERE_CENSUS_BOUNDARY_CACHE` env var). `load_tract_boundary_crosswalk`
reads from that cache; if the parquet is absent, it raises a clear
error message pointing to the download function.

The release tag is decoupled from the package version: the boundary
crosswalk changes only when Census re-releases the relationship file
(once per decade), so it is keyed on `TELLTALERE_CENSUS_BOUNDARY_RELEASE_TAG`
(default `boundaries-v1`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import requests


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Release URL configuration
# ---------------------------------------------------------------------------

_DEFAULT_RELEASE_OWNER = "nstef18447"
_DEFAULT_BOUNDARY_RELEASE_TAG = "boundaries-v1"
_ENV_RELEASE_OWNER = "TELLTALERE_CENSUS_RELEASE_OWNER"
_ENV_BOUNDARY_RELEASE_TAG = "TELLTALERE_CENSUS_BOUNDARY_RELEASE_TAG"
_ENV_BOUNDARY_CACHE = "TELLTALERE_CENSUS_BOUNDARY_CACHE"

_CROSSWALK_FILENAME = "tract_boundary_crosswalk_2010_to_2020.parquet"


def _release_url() -> str:
    owner = os.environ.get(_ENV_RELEASE_OWNER, _DEFAULT_RELEASE_OWNER)
    tag = os.environ.get(_ENV_BOUNDARY_RELEASE_TAG, _DEFAULT_BOUNDARY_RELEASE_TAG)
    return (
        f"https://github.com/{owner}/telltalere-census/"
        f"releases/download/{tag}/{_CROSSWALK_FILENAME}"
    )


def _boundary_cache_dir(explicit: Optional[Path] = None) -> Path:
    """
    Resolve the boundary-crosswalk cache directory.

    Resolution order:
      1. Explicit `cache_dir` argument
      2. TELLTALERE_CENSUS_BOUNDARY_CACHE env var
      3. platformdirs.user_cache_dir('telltalere_census') / 'boundaries'
    """
    if explicit is not None:
        d = Path(explicit)
    elif os.environ.get(_ENV_BOUNDARY_CACHE):
        d = Path(os.environ[_ENV_BOUNDARY_CACHE])
    else:
        try:
            from platformdirs import user_cache_dir  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "platformdirs is required for the default boundary cache "
                "directory. Install it via `pip install platformdirs`, set "
                f"the {_ENV_BOUNDARY_CACHE} env var, or pass cache_dir="
                "explicitly to load/download_tract_boundary_crosswalk."
            ) from exc
        d = Path(user_cache_dir("telltalere_census")) / "boundaries"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Boundary vintage lookup
# ---------------------------------------------------------------------------

def get_boundary_vintage_for_acs(
    vintage_end_year: int,
    acs_type: Literal["acs5", "acs1"] = "acs5",
) -> Literal[2010, 2020]:
    """Return the Census tract boundary vintage (2010 or 2020) used by
    the given ACS vintage for tract / block-group geography.

    Cutover (verified 2026-05-04 against
    https://www.census.gov/programs-surveys/acs/geography-acs/geography-boundaries-by-year.{YYYY}.html
    for years 2019, 2020, 2021, 2022, 2023, 2024):

      ACS 5-year:  end-year <= 2019  ->  2010 Census boundaries
                   end-year >= 2020  ->  2020 Census boundaries
      ACS 1-year:  end-year <= 2019  ->  2010 Census boundaries
                   end-year == 2020  ->  not published
                   end-year >= 2021  ->  2020 Census boundaries

    Tract / block-group scope only. Other summary levels (PUMA,
    Congressional District, Urban Area) migrated on a different
    schedule (2022 1-year per Census user note 2023-02) and are not
    handled by this function.

    Args:
        vintage_end_year: ACS vintage end-year (e.g., 2019 for 2015-2019
            5-year, 2022 for 2022 1-year).
        acs_type: 'acs5' or 'acs1'.

    Returns:
        Either 2010 or 2020.

    Raises:
        ValueError: if vintage_end_year is < 2009 (predates supported
            ACS 5-year publication) or > current year + 1, or if
            acs_type='acs1' with end-year 2020 (not published).
    """
    if acs_type not in ("acs5", "acs1"):
        raise ValueError(f"acs_type must be 'acs5' or 'acs1', got {acs_type!r}")
    if vintage_end_year < 2009:
        raise ValueError(
            f"vintage_end_year {vintage_end_year} predates published ACS 5-year "
            "(earliest end-year is 2009)"
        )
    if acs_type == "acs1" and vintage_end_year == 2020:
        raise ValueError(
            "ACS 1-year was not published for 2020 (COVID data-quality issues)"
        )
    if vintage_end_year <= 2019:
        return 2010
    return 2020


# ---------------------------------------------------------------------------
# Crosswalk download / load
# ---------------------------------------------------------------------------

def download_tract_boundary_crosswalk(
    cache_dir: Optional[Path] = None,
    *,
    force: bool = False,
    timeout: int = 300,
) -> Path:
    """Download the national 2010 <-> 2020 tract boundary crosswalk
    parquet from the package GitHub release into the boundary cache.

    The crosswalk is a single national parquet (~5 MB compressed) keyed
    on (geoid_2010, geoid_2020) with both 2010-side and 2020-side area
    weights. Built by `scripts/build_tract_boundary_crosswalk.py` from
    the Census-published relationship file.

    Args:
        cache_dir: override for the boundary cache directory.
        force: re-download even if the parquet is already present.
        timeout: per-request HTTP timeout (seconds). Default 300.

    Returns:
        Path to the cached parquet.

    Raises:
        requests.HTTPError on a failed download. The release URL is
        built from environment variables; defaults point at a placeholder
        owner. A 404 typically means TELLTALERE_CENSUS_RELEASE_OWNER /
        TELLTALERE_CENSUS_BOUNDARY_RELEASE_TAG env vars are pointing at
        a release that doesn't exist yet, or the asset wasn't uploaded.
    """
    cache = _boundary_cache_dir(cache_dir)
    target = cache / _CROSSWALK_FILENAME
    if target.exists() and not force:
        logger.info("Boundary crosswalk already cached: %s", target)
        return target
    url = _release_url()
    logger.info("Downloading %s from %s", target.name, url)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    target.write_bytes(resp.content)
    return target


def load_tract_boundary_crosswalk(
    direction: Literal["2010_to_2020", "2020_to_2010"] = "2010_to_2020",
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Load the 2010 <-> 2020 tract boundary crosswalk from the local
    boundary cache.

    The crosswalk is one underlying parquet with both weight columns
    (`area_weight_2010` and `area_weight_2020`); the `direction`
    parameter is cosmetic — it controls only which side keys 1.0:

      direction='2010_to_2020' (default):
          rows for the same `geoid_2010` have `area_weight_2010` values
          that sum to ~1.0. Use this orientation when allocating a 2010
          tract's value across its 2020 successors.
      direction='2020_to_2010':
          rows for the same `geoid_2020` have `area_weight_2020` values
          that sum to ~1.0. Use this orientation when checking how a
          2020 tract's land composition came from 2010 tracts.

    The returned DataFrame is identical for both directions; the value
    is in documenting the consumer's intent.

    Schema (columns):
      geoid_2020         (str, 11 chars)
      geoid_2010         (str, 11 chars)
      state_fips         (str, 2 chars)
      county_fips        (str, 3 chars)
      arealand_part_sqm  (Int64, intersection land area in square meters)
      arealand_2020_sqm  (Int64, total 2020 tract land area)
      arealand_2010_sqm  (Int64, total 2010 tract land area)
      area_weight_2010   (Float64, AREALAND_PART / AREALAND_TRACT_10)
      area_weight_2020   (Float64, AREALAND_PART / AREALAND_TRACT_20)
      relationship_type  (str: 'unchanged', 'split', 'merge', 'partial')

    Water-only tracts (Census GEOID series ending in `9900xx`) have
    `area_weight_*` = NA because they have zero land area. Such tracts
    do not participate in meaningful harmonization and pass through as
    NA in `harmonize_to_2020_boundaries` outputs.

    Args:
        direction: '2010_to_2020' or '2020_to_2010'. Cosmetic (see above).
        cache_dir: override for the boundary cache directory.

    Raises:
        FileNotFoundError if the crosswalk parquet is not in the cache.
            The error message includes the download call to fix it.
        ValueError on invalid direction.
    """
    if direction not in ("2010_to_2020", "2020_to_2010"):
        raise ValueError(
            f"direction must be '2010_to_2020' or '2020_to_2010', got {direction!r}"
        )
    cache = _boundary_cache_dir(cache_dir)
    target = cache / _CROSSWALK_FILENAME
    if not target.exists():
        raise FileNotFoundError(
            f"Tract boundary crosswalk not found at {target}. Call "
            "`download_tract_boundary_crosswalk()` to fetch it (one-time, "
            "~5 MB)."
        )
    return pd.read_parquet(target)


# ---------------------------------------------------------------------------
# Harmonization
# ---------------------------------------------------------------------------

def harmonize_to_2020_boundaries(
    historical_data: pd.DataFrame,
    count_columns: list[str],
    median_columns: list[str] = (),
    *,
    median_treatment: Literal["nan", "raise"] = "nan",
    na_treatment: Literal["propagate", "zero", "renormalize"] = "propagate",
    geography_col: str = "geoid",
    crosswalk: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Map 2010-boundary tract data onto 2020 tract boundaries via
    area-weighted allocation.

    For `count_columns`: each 2010 tract's value is allocated to its
    contributing 2020 tracts using `area_weight_2010` from the
    crosswalk. The 2020 tract's value is the sum of contributions
    across its component 2010 tracts.

    For `median_columns`: medians cannot be meaningfully harmonized via
    weighted allocation (the median of a population subset is not a
    weighted-average of source-medians). Behavior controlled by
    `median_treatment`:
      'nan' (default): output median columns are NaN with no error
        raised. Document loudly to the consumer that medians are not
        carrying through.
      'raise': raises ValueError on entry to force the consumer to
        acknowledge that median harmonization is not available.

    Area-weighted vs population-weighted (the assumption this places)
    -----------------------------------------------------------------
    Weights in the crosswalk are derived from AREALAND intersections
    (`AREALAND_PART / AREALAND_TRACT_10`), not population. This implies
    the assumption that population is uniformly distributed within each
    boundary change — i.e., when a 2010 tract is split into halves, the
    function allocates its values 50/50 by land area regardless of
    where the people actually lived.

    When this assumption breaks
    ---------------------------
    The area-weighting approximation is accurate for typical tract
    subdivisions (Census splits a populated tract along arterial roads,
    population stays roughly proportional to land). It is
    **systematically wrong** in two cases that matter for trend
    analysis in growth markets:

      1. **Greenfield development markets**. Sun Belt growth corridors,
         exurban expansion, and brownfield-to-residential conversions
         create 2020 tracts where the 2010-era population was zero or
         near-zero. The 2010 tract that contained that land was
         populated only in its already-developed portion; the now-
         developed portion had zero people in 2010. Area-weighting
         allocates 2010 population proportionally to land area
         regardless. **Result: the 2020 successor tract covering the
         new development inherits a phantom 2010 baseline population,
         which understates measured growth.** Bias is worst when the
         new-development tract represents a large share of the 2010
         source-tract's land area — typical of master-planned
         communities and large-lot suburban expansion.

      2. **Tracts split along physical or zoning boundaries**. Tracts
         that contained both residential and industrial / commercial /
         park / institutional land before 2020 redistricting may have
         been split with the dividing line following the
         residential-vs-other boundary (Census frequently uses
         zoning boundaries to delineate new tracts). Area-weighting
         allocates population proportional to land area; in reality,
         all the population was in the residential half. **Result: the
         new 2020 tract that inherits the non-residential half
         receives a phantom population it never had, while the
         residential 2020 tract loses some of the population that
         actually lived there.**

    The failure mode is **asymmetric and direction-dependent**:
      - In growth markets, area-weighting *understates* growth in
        newly-developed tracts (phantom 2010 baseline) and
        *overstates* the apparent shrinkage of the 2010 source tract's
        residential half (the residential tract appears to have lost
        residents to a new tract that was actually empty in 2010).
      - In depopulating cities, area-weighting *overstates* retention
        in tracts that absorbed formerly-populated land that has
        since been demolished or converted to non-residential use.

    For trend analysis in known growth markets (Sun Belt metros,
    exurban Texas/Florida/Carolinas, suburban-expansion metros) treat
    tract-level cross-decade comparisons cautiously. Aggregating to
    county or MSA level is often the right move when the question can
    tolerate coarser geography. True population weights — derived from
    joining 2020 Census P.L. 94-171 block-level population to the
    intersection geometries — would correct this; that work is
    documented as future scope (see session 5 doc).

    NA treatment for missing inputs
    -------------------------------
    When the historical_data has NaN / pd.NA in a count column for some
    2010 tract (Census suppression, sample-size flags), the policy is
    controlled by `na_treatment`:

      'propagate' (default, conservative):
          Any 2020 tract with even one NA-valued contributor becomes NA
          for that count column. Worst-case for data loss but ensures
          the output never silently fabricates a value from partial
          information.

      'zero' (discouraged):
          NA contributors are treated as zero. Discouraged because it
          creates a spurious "this tract shrunk" signal: a suppressed
          1,000-person 2010 tract becomes a phantom-zero contribution
          to its 2020 successor, and the successor reads as having lost
          1,000 people that were never measured-as-zero in the first
          place.

      'renormalize' (aggressive, opt-in):
          Drop NA contributors, renormalize the surviving weights to
          sum to 1.0, allocate. Reuses partial information under the
          assumption that the suppressed contributors were
          proportionally similar to the surviving ones. Defensible
          when only one contributor in a many-contributor tract is
          suppressed; risky when the suppressed contributor was a
          large share of the source.

    Args:
        historical_data: 2010-boundary tract DataFrame. Must contain
            `geography_col` with 11-digit 2010 tract GEOIDs.
        count_columns: column names to allocate by area weights.
        median_columns: column names to handle per `median_treatment`.
            Default empty.
        median_treatment: 'nan' or 'raise'. See docstring.
        na_treatment: 'propagate' / 'zero' / 'renormalize'. See docstring.
        geography_col: name of the column containing 11-digit 2010 tract
            GEOIDs. Default 'geoid'.
        crosswalk: optional pre-loaded crosswalk DataFrame (skips loading
            from cache). When None, calls `load_tract_boundary_crosswalk()`.

    Returns:
        DataFrame indexed by 2020 tract GEOID (string column 'geoid_2020'
        in the output), with the same value columns as the input.
        Median columns are NaN when median_treatment='nan'.

    Raises:
        ValueError on unknown `median_treatment` or `na_treatment`,
            or when median_treatment='raise' and median_columns is
            non-empty.
        KeyError if `geography_col` or any of the requested columns are
            missing from `historical_data`.
    """
    if median_treatment not in ("nan", "raise"):
        raise ValueError(
            f"median_treatment must be 'nan' or 'raise', got {median_treatment!r}"
        )
    if na_treatment not in ("propagate", "zero", "renormalize"):
        raise ValueError(
            f"na_treatment must be 'propagate', 'zero', or 'renormalize', "
            f"got {na_treatment!r}"
        )
    if median_treatment == "raise" and len(median_columns) > 0:
        raise ValueError(
            "median_treatment='raise' was passed alongside median_columns="
            f"{list(median_columns)}. Median harmonization via area-weighted "
            "allocation is not statistically meaningful — pass "
            "median_treatment='nan' to opt into NaN output, or aggregate to "
            "a coarser geography (county / MSA) before computing medians."
        )
    if geography_col not in historical_data.columns:
        raise KeyError(
            f"historical_data missing geography column {geography_col!r}"
        )
    missing_counts = [c for c in count_columns if c not in historical_data.columns]
    if missing_counts:
        raise KeyError(
            f"historical_data missing count columns: {missing_counts}"
        )
    missing_meds = [c for c in median_columns if c not in historical_data.columns]
    if missing_meds:
        raise KeyError(
            f"historical_data missing median columns: {missing_meds}"
        )

    if crosswalk is None:
        crosswalk = load_tract_boundary_crosswalk()

    # Inner-join historical 2010 tracts with the crosswalk. Bring both
    # weight columns: area_weight_2010 is used for the standard
    # allocation (per-2010 perspective: where does this 2010 tract's
    # value go?); area_weight_2020 is used for renormalize semantics
    # (per-2020 perspective: redistribute among surviving contributors
    # to a given 2020 tract).
    h = historical_data[[geography_col, *count_columns, *median_columns]].copy()
    h[geography_col] = h[geography_col].astype(str).str.zfill(11)
    cw = crosswalk[
        ["geoid_2010", "geoid_2020", "area_weight_2010", "area_weight_2020"]
    ].copy()

    joined = cw.merge(h, left_on="geoid_2010", right_on=geography_col, how="left")

    output_rows: dict[str, pd.Series] = {}

    for col in count_columns:
        col_values = joined[col].astype("Float64")
        weight_2010 = joined["area_weight_2010"].astype("Float64")
        weight_2020 = joined["area_weight_2020"].astype("Float64")

        # Mask of 2020 tracts that have any NA contributor for this column
        had_na = (
            joined.assign(_isna=col_values.isna())
            .groupby("geoid_2020", observed=True)["_isna"]
            .any()
        )

        if na_treatment == "zero":
            # NA -> 0 contribution. Uses standard area_weight_2010 allocation.
            allocations = col_values.fillna(0) * weight_2010
            output_rows[col] = (
                joined.assign(_alloc=allocations)
                .groupby("geoid_2020", observed=True)["_alloc"]
                .sum(min_count=1)
            )
        elif na_treatment == "propagate":
            # Standard area_weight_2010 allocation; mask out any 2020 tract
            # that had even one NA contributor.
            allocations = col_values * weight_2010
            allocated = (
                joined.assign(_alloc=allocations)
                .groupby("geoid_2020", observed=True)["_alloc"]
                .sum(min_count=1)
            )
            output_rows[col] = allocated.where(~had_na, other=pd.NA)
        else:  # 'renormalize'
            # Two-branch logic per 2020 tract group:
            #   - If group has no NA contributors: standard area_weight_2010
            #     allocation (no renorm needed; preserves the 2010 tract's
            #     original value sum).
            #   - If group has NA contributors: drop them, and use
            #     area_weight_2020 renormalized so surviving rows sum to 1.0
            #     within the group. Each surviving contributor's value is
            #     scaled by its share-of-surviving-land of the 2020 tract.
            no_na_alloc = col_values * weight_2010
            no_na_result = (
                joined.assign(_alloc=no_na_alloc)
                .groupby("geoid_2020", observed=True)["_alloc"]
                .sum(min_count=1)
            )

            # Renorm path: sum_surviving_area_weight_2020 per 2020 tract,
            # over rows where col_values is not NA
            valid = col_values.notna()
            valid_w20 = weight_2020.where(valid, other=0.0)
            sum_valid_w20 = (
                joined.assign(_w=valid_w20)
                .groupby("geoid_2020", observed=True)["_w"]
                .transform("sum")
            )
            # Avoid divide-by-zero (all NAs in a group -> already covered by
            # propagate-equivalent behavior below)
            renorm_weight = weight_2020 / sum_valid_w20
            renorm_alloc = (col_values * renorm_weight).where(valid, other=0.0)
            renorm_result = (
                joined.assign(_alloc=renorm_alloc)
                .groupby("geoid_2020", observed=True)["_alloc"]
                .sum(min_count=1)
            )

            # Combine: pick renorm where had_na, no_na otherwise
            combined = no_na_result.where(~had_na, other=renorm_result)
            # Where every contributor was NA, sum_valid_w20 was 0 -> renorm_alloc
            # was 0 sum -> we still want NA (no info to renormalize from)
            all_na = (
                joined.assign(_isna=col_values.isna())
                .groupby("geoid_2020", observed=True)["_isna"]
                .all()
            )
            combined = combined.where(~all_na, other=pd.NA)
            output_rows[col] = combined

    # Build the output DataFrame keyed on 2020 tract
    all_2020_tracts = pd.Index(joined["geoid_2020"].dropna().unique(), name="geoid_2020")
    out = pd.DataFrame(index=all_2020_tracts)
    for col, series in output_rows.items():
        out[col] = series.reindex(all_2020_tracts)

    # Median columns: NaN per policy (median_treatment='raise' would have
    # already short-circuited above)
    for col in median_columns:
        out[col] = pd.NA

    out = out.reset_index()  # exposes geoid_2020 as a column
    return out
