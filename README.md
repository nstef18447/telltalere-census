# telltalere-census

Shared Census data layer for Telltale RE products. Handles Census ACS PUMS microdata
fetch + caching, ACS5 block-group summary fetch, PUMA/county/CBSA crosswalks, and
domain-neutral derived fields used by downstream real estate products.

This package is consumed via **editable install** by sibling product repos
(`ami-tool`, `Core_BTR`). Nothing in this package knows about AMI, HUD income
limits, CoStar, Zillow, or any product-specific logic — those live in the
product repos.

## Install

From a product repo, add to `requirements.txt`:

```
-e ../telltalere-census
```

Then `pip install -r requirements.txt`.

## Modules

| Module | Purpose |
| --- | --- |
| `pums_fetch` | Census ACS PUMS API fetch, split H / P records, joined on SERIALNO, parquet caching |
| `acs_fetch` | Census ACS summary-table fetch at block-group or tract geography, variable-list driven, MOE auto-pairing, chunked for ≤50-var API limit, parquet caching |
| `county` | County name / FIPS resolution via Census geography API |
| `puma_crosswalk` | PUMA ↔ county crosswalk + population-weighted allocation |
| `cbsa_crosswalk` | County ↔ CBSA crosswalk |
| `weights` | Weight-aware aggregation helpers (weighted sums, weighted averages, groupby-weighted) |
| `derived_fields` | Domain-neutral derived columns: cost burden, age cohort, tenure, structure type, building era, bedroom tier, household type, MV-based buckets, education, employment, poverty flags, etc. |
| `variables` | PUMS / ACS5 variable code constants, whitelists, and bucketing lookups |
| `cache` | Parquet cache conventions (filename scheme, vintage handling) |
| `binning` | Generic ACS bracket-cell → display-bin rollup mechanism (the dictionaries themselves stay in consumer projects) |
| `bracket_allocation` | Proportional reallocation of ACS bracketed counts onto a target interval (open-ended top bracket aware) |
| `tract_marginals` | Tenure-/bin-agnostic tract or block-group marginal computation, plus subtotal MOE flag helper |
| `puma_crosstab` | Pre-binned PUMS aggregation along arbitrary dimensions, with adapters to/from the column-major dict shape consumed by `ipf` |
| `ipf` | 2-D iterative proportional fitting (matrix raking) — rake a PUMS-derived joint to ACS marginals to synthesize tract-level joints |
| `geometry` | Tract polygon + tract-to-PUMA crosswalk loaders. Crosswalk ships in wheel; polygons are runtime-downloaded per state via `download_tract_polygons` |

## Variable whitelist pattern

Both `pums_fetch.fetch_pums_data()` and `acs_fetch.fetch_acs_data()` take a list
of variable codes as an argument. They do not hardcode which variables to fetch.
Sensible defaults are exported from `variables`:

```python
from telltalere_census.variables import (
    PUMS_DEFAULT_HOUSING_VARS,
    PUMS_DEFAULT_PERSON_VARS,
    ACS_BG_DEFAULT_VARS,
)
```

Downstream products may pass their own lists to fetch only what they need.

## Cache conventions

Parquet files on disk use vintage-in-filename so multiple vintages can coexist
without manual invalidation:

- `pums_{acs_type}_{acs_year}_{state_fips}_H.parquet`
- `pums_{acs_type}_{acs_year}_{state_fips}_P.parquet`
- `acs5_{year}_{state_fips}_bg.parquet`
- `acs5_{year}_{state_fips}_tract.parquet`
- `tract_pop_2020_{state_fips}.parquet`

Cache directory defaults to `./data/` relative to the process CWD. Override by
passing `cache_dir=Path(...)` to fetch functions, or by setting the
`TELLTALERE_CENSUS_CACHE_DIR` env var.

To invalidate a cached file, delete it from disk and re-run.

## API keys

`CENSUS_API_KEY` — optional. Recommended for >500 queries/day. Read lazily by
fetch functions; never validated at import time.

## Tract-level synthesis primitives (session 3)

The package ships generic, tenure-agnostic primitives for synthesizing
tract-level joint distributions from PUMA-level PUMS priors and tract-level
ACS marginals. Composition for the typical "renter income × age" use case:

```python
from telltalere_census import (
    BracketConfig, compute_tract_marginal,
    compute_puma_joint, joint_to_column_major,
    rake_to_marginals,
)

# 1. Roll up ACS bracket cells into tract-level display bins
income_cfg = BracketConfig(
    denominator_var="B25118_014E",                    # renter-income subtotal
    bracket_to_bin={"B25118_015E": "under_35k", ...}, # consumer-supplied
    bin_order=["under_35k", "35_50k", ..., "150k_plus"],
)
income_marginal = compute_tract_marginal(tract_row, income_cfg)
age_marginal    = compute_tract_marginal(tract_row, age_cfg)

# 2. PUMA-level joint from PUMS (caller pre-bins + supplies tenure filter)
joint_df = compute_puma_joint(
    pre_binned_pums_df,
    dimensions=["income_bin", "age_bin"],
    filter_func=lambda d: d["TEN"].isin([3, 4]),  # renters
)
prior = joint_to_column_major(
    joint_df, row_dim="income_bin", col_dim="age_bin",
    row_order=INCOME_BINS, col_order=AGE_BINS,
)

# 3. Rake the PUMA prior to the tract marginals
result = rake_to_marginals(prior, income_marginal, age_marginal)
synthesized_joint = result["joint"]    # column-major dict
```

All primitives are tenure-/bin-agnostic — the bracket configurations,
bin schemes, and tenure filters live in consumer projects. The package
supplies the mechanism only.

The same primitives ported from Core_BTR's BTR pipeline produce
**byte-identical output** to that pipeline (verified across 160 Lake
County tracts × 3 dimensions in `tests/test_core_btr_parity.py`).

## Geometry: tract polygons + tract-to-PUMA crosswalk

```python
from telltalere_census import (
    load_tract_to_puma_crosswalk,
    load_tract_polygons,
    download_tract_polygons,
    wkb_to_geojson,
    load_tract_polygons_as_gdf,    # requires [geo] extra
)

# Crosswalk ships in the wheel — zero-cost load
xwalk = load_tract_to_puma_crosswalk(state_fips="17")

# Polygons do NOT ship in the wheel — fetch on demand into a user cache
download_tract_polygons(state_fips=["17", "18"])
polygons = load_tract_polygons(state_fips="17")
geojson = wkb_to_geojson(polygons["polygon_wkb"].iloc[0])
```

Polygons come from the TIGERweb 1:500k generalized boundaries, with
`shapely.simplify(0.0005)` applied at build time. Per-state parquet
files are uploaded as GitHub release assets and downloaded on demand.

Cache resolution for downloaded polygons:
1. explicit `cache_dir` argument
2. `TELLTALERE_CENSUS_GEOMETRY_CACHE` env var
3. `platformdirs.user_cache_dir("telltalere_census") / "geometry"`

Release URL configuration (set once after the GitHub release is
published):
- `TELLTALERE_CENSUS_RELEASE_OWNER` (default placeholder)
- `TELLTALERE_CENSUS_RELEASE_TAG`   (default `v{__version__}`)

For GeoDataFrame loading, install the `[geo]` extra:
```
pip install telltalere-census[geo]
```

## Known gotchas

- **PUMA field name change**: 2023+ vintages use `PUMA`; 2022 and earlier ACS5
  vintages use `PUMA20`. The PUMS fetch normalizes on the way out — callers
  always see `PUMA`.
- **MIG has 3 values, not 4**: `1=same house`, `2=from abroad`, `3=different US
  house`. Older docs referring to value `4` are wrong for modern vintages.
- **YRBLT vs YBL**: 2024 ACS5 uses `YRBLT` (actual years). `YBL` does not exist
  in 2024 ACS5 — don't request it.
- **RELSHIPP vs RELP**: 2024 ACS5 uses `RELSHIPP`. `RELP` does not exist.
- **CT planning regions**: Connecticut's reorganized county FIPS codes are not
  recognized by some upstream data sources (HUD). That's a product-level
  concern handled in `ami-tool`, but be aware when using CT in PUMS results.
- **Small-county sample thresholds**: PUMS sample sizes in small counties can
  be low. Downstream products apply their own minimum-sample rules (e.g. N>=20
  for per-bedroom averages); this package does not filter.
- **1-year ACS PUMA predicate**: Census API does not accept PUMA as a filter
  predicate on 1-year vintage. The fetch pulls full-state and filters
  client-side (same as 5-year API behavior when PUMA list is long).

## `acs_fetch` usage

```python
from telltalere_census.acs_fetch import fetch_acs_data
from telltalere_census.variables import ACS_BG_DEFAULT_VARS

# Block-group fetch for Illinois, with MOE companions auto-paired
df = fetch_acs_data(
    state_fips="17",
    variables=ACS_BG_DEFAULT_VARS,      # 45 estimate vars; MOE pairing → 90 codes
    geography="block group",             # or "tract"
    vintage=2024,
    acs_type="acs5",
    include_moe=True,                    # default; adds `_M` column per `_E`
)

# Filter to a county
lake = df[df["GEOID"].str.startswith("17097")]
```

### What the fetcher handles for you

- **MOE auto-pairing** — every `_E` estimate variable is paired with its
  `_M` counterpart when `include_moe=True` (default). Both columns appear
  in the returned DataFrame.
- **Variable chunking** — Census ACS API limits `get=` lists to 50
  variables per call. `fetch_acs_data` sorts variables, splits into
  deterministic chunks, fetches each chunk independently, and
  inner-joins on the composite GEOID. A geography-row mismatch across
  chunks is logged as a warning, not silently outer-joined.
- **Special-value normalization** — Census sentinels
  (`-555555555`, `-222222222`, `-333333333`, `-666666666`,
  `-888888888`, `-999999999`, `"*"`, `"**"`, `"(X)"`, `"null"`) are
  mapped to pandas NA. Downstream code can use `.isna()` uniformly.
- **Nullable dtypes** — estimate and MOE columns use nullable `Int64`
  for whole-number values or `Float64` for fractional (e.g. avg
  household size). Never raw float-with-NaN.
- **GEOID construction** — composite key built from geography atoms:
  `state+county+tract+block_group` (12 chars) for block group, or
  `state+county+tract` (11 chars) for tract.
- **Caching** — one parquet per `(state, vintage, geography)`. A cache
  that's missing any requested variable triggers a fresh fetch of the
  UNION of cached + newly requested variables, written back as one
  coherent snapshot (no partial deltas).

### API pattern notes

Tested against 2024 ACS5 on 2026-04-19:

- **Block group** queries require `in=state:{st} county:*`. State-only
  `in=state:{st}` returns `400 unknown/unsupported geography hierarchy`.
- **Tract** queries accept `in=state:{st}` directly.

Both patterns return the full state in a single API call per chunk.

## Testing

```bash
pytest tests/                          # unit + integration
pytest tests/ -m "not integration"     # skip live-API tests
```

The Marion County benchmark (`tests/test_marion_benchmark.py`) is the
regression gate for the PUMS pipeline. It compares post-extraction
results against a baseline captured from the pre-extraction ami-tool
pipeline for Marion IN (FIPS 18097, 2024 ACS5) — exact equality on
weighted totals, per-PUMA breakdowns, and derived field distributions.

The Lake County integration test
(`tests/test_acs_fetch_lake_county.py`, marked `@pytest.mark.integration`)
is the regression gate for `acs_fetch`. It fetches Illinois at both
block-group and tract geography and validates Lake County totals
(B25003_001E) against the authoritative county-level 2024 ACS5 value of
258,455 occupied housing units, with 5% tolerance.
