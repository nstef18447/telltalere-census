# Session 5 — Multi-vintage ACS, trend computation, tract boundary harmonization

Status: **complete**.

## Goal

Add the time dimension to the package. Sessions 3 and 4 built cross-tenure
demographic primitives at a single point in time; session 5 ships:

1. **Multi-vintage ACS fetch** — wraps `acs_fetch.fetch_acs_data` to support
   multiple vintages in one call. Long-format DataFrame with a `vintage`
   column. Caching, chunking, MOE pairing reuse the single-vintage path.
2. **Trend computation primitives** — pure-function metrics over
   `dict[int, float]` inputs. Absolute change, percent change, CAGR,
   direction flag, MOE-aware significance, cumulative time-series
   trend.
3. **2010 ↔ 2020 tract boundary harmonization** — opt-in mapping of
   pre-cutover tract-level data onto 2020 tract boundaries via area-
   weighted allocation. Three NA-handling modes. Refuses to harmonize
   medians.

Core_BTR is unmodified. Marion benchmark and sessions 3/4 byte-identity
gates remain green.

## What shipped

### `variables.py`

`B01003_001E` (total population) added to `ACS_BG_DEFAULT_VARS` at the
top of the list. MOE auto-pairing brings the request from 170 → 172
vars, still 4 chunks under the 50-var-per-call API ceiling.

Lake County tract fixture regenerated under the expanded variable list;
`B25003_002 + _003 == _001` and `sum(owner B25118 brackets) == _002E`
invariants still exact across all 160 tracts.

### `multi_vintage.py` (new)

Two functions:

- **`fetch_acs_data_multi_vintage(state_fips, variables, vintages,
  geography, acs_type, include_moe, cache_dir)`**
  Loops `fetch_acs_data` over each vintage, adds a `vintage` int column,
  `pd.concat`s into a long-format DataFrame. Validates vintages against
  `get_available_vintages` *before* any fetch — fails fast on typos.
  Sorts and deduplicates the input vintage list.

- **`get_available_vintages(geography, acs_type)`**
  Hardcoded availability lists last verified 2026-05-04:
  - ACS5: end-years 2013–2024 inclusive
  - ACS1: end-years 2014–2024 inclusive minus 2020 (Census did not
    publish 2020 1-year due to COVID data-quality issues)
  - Block-group and tract require ACS5 (ACS1 doesn't publish either)

Cache reuses single-vintage parquets (`acs5_{vintage}_{state}_tract.parquet`)
unchanged. Multi-vintage is a stateless wrapper.

**Inflation-comparability caveat documented prominently in the module
docstring.** Income-bracket variables (B25118, B19001, etc.) and dollar-
denominated variables (B25064 median rent, B25077 median home value,
etc.) across vintages are vintage-native nominal dollars. ACS does not
adjust historical estimates for inflation. CPI deflation is not provided
by this package — consumers apply at their own layer. Out of scope as of
v0.3.0.

18 unit tests with mocked single-vintage path; live multi-vintage fetch
covered by the e2e integration test.

### `trends.py` (new)

Pure-function trend primitives over `dict[int, float]` inputs.

- **`TrendResult`** TypedDict with `early_value`, `late_value`,
  `early_vintage`, `late_vintage`, `years_elapsed`, `absolute_change`,
  `percent_change`, `cagr`, `direction`, `is_significant`,
  `moe_combined`.

- **`cagr(early, late, years) -> float | None`**
  Returns None on undefined inputs (early ≤ 0, late < 0, years ≤ 0,
  NaN). late = 0 is allowed (returns -1.0, "100% loss per year").

- **`is_change_significant(early, early_moe, late, late_moe, method)`**
  Two methods:
  - `moe_combined` (Census Bureau's recommended approach, default):
    `|late - early| > sqrt(early_moe² + late_moe²)`
  - `moe_overlap` (CIs do not overlap; geometric / commonly visualized)
  combined-MOE is strictly tighter for the typical positive-MOE case.

- **`compute_trend_metrics(values_by_vintage, moes_by_vintage,
  direction_threshold, significance_method) -> TrendResult`**
  Uses earliest and latest vintages only. Direction classification:
  - `"increasing"` if pct_change > +threshold
  - `"decreasing"` if pct_change < -threshold
  - `"flat"` if |pct_change| ≤ threshold AND is_significant
  - `"noise"` if |pct_change| ≤ threshold AND NOT is_significant
  Edge cases for early ≤ 0 or NaN documented and tested.

- **`cumulative_trend(values_by_vintage, ...) -> list[TrendResult]`**
  Consecutive-pair results, length N-1.

36 unit tests covering hand-computable cases (5-year doubling → CAGR
~14.87%), edge cases, combined-vs-overlap method divergence, all
direction states.

### `boundaries.py` (new)

Three primitives + a runtime-download pattern.

- **`get_boundary_vintage_for_acs(end_year, acs_type) -> 2010 | 2020`**
  Tract / BG cutover lookup. Verified against Census's "Geography
  Boundaries by Year" for 2019 through 2024 on 2026-05-04:
  - ACS5: end-year ≤ 2019 → 2010; ≥ 2020 → 2020
  - ACS1: end-year ≤ 2019 → 2010; ≥ 2021 → 2020 (2020 1-year not
    published)
  Scoped to tract/BG only. PUMA / Congressional District / Urban Area
  migrations are different (2022 1-year per Census user note 2023-02).

- **`download_tract_boundary_crosswalk(cache_dir, force, timeout) -> Path`**
  Mirrors `geometry.download_tract_polygons` exactly. URL built from
  env vars (`TELLTALERE_CENSUS_RELEASE_OWNER`,
  `TELLTALERE_CENSUS_BOUNDARY_RELEASE_TAG` defaulting to
  `boundaries-v1`). Cache resolves via `TELLTALERE_CENSUS_BOUNDARY_CACHE`
  or `platformdirs.user_cache_dir/'boundaries'`.

- **`load_tract_boundary_crosswalk(direction, cache_dir) -> pd.DataFrame`**
  Reads the cached parquet. `direction` parameter is cosmetic — both
  weight columns (`area_weight_2010`, `area_weight_2020`) are always
  present; `direction` documents the consumer's intent.

- **`harmonize_to_2020_boundaries(historical_data, count_columns,
  median_columns, median_treatment, na_treatment, geography_col,
  crosswalk) -> pd.DataFrame`**
  Area-weighted allocation. For count columns: each 2010 tract's value
  is allocated to its 2020 successors using `area_weight_2010`. For
  median columns: refuses to invent harmonized medians; default
  `median_treatment="nan"` outputs NaN, `"raise"` short-circuits.
  Three NA modes:
  - `"propagate"` (default, conservative): any 2020 tract with even
    one NA contributor → NA
  - `"zero"` (discouraged): NA contributors → 0 contribution. Creates
    spurious "tract shrunk" signal.
  - `"renormalize"` (aggressive, opt-in): drop NA contributors, use
    `area_weight_2020` renormalized to surviving sum within each
    2020 tract group. No-op when no NAs in a group.

**Honest column naming.** The brief specified `population_weight_*`,
but the underlying Census relationship file contains AREALAND
intersections, not population. Naming corrected to `area_weight_*`
to match the data. True population weighting would require joining
2020 P.L. 94-171 block populations — significant scope expansion
documented as future work.

**"When this assumption breaks" caveat in the harmonize docstring.**
Area-weighting fails systematically in:
1. **Greenfield development markets** (Sun Belt corridors, exurban
   expansion, brownfield-to-residential): the 2020 successor tract
   covering new development inherits a phantom 2010 baseline
   population, *understating* measured growth. Bias worst when the
   new-development tract is a large share of the 2010 source-tract's
   land area.
2. **Tracts split along zoning / physical boundaries**
   (residential vs. industrial / commercial / institutional halves):
   area-weighting allocates population proportional to land area; in
   reality all population was in the residential half. The new 2020
   tract inheriting the non-residential half receives phantom
   population it never had.

Failure is asymmetric: in growth markets, area-weighting *understates*
growth in newly-developed tracts; in depopulating cities it
*overstates* retention in tracts that absorbed formerly-populated
land. For trend analysis in known growth markets, treat tract-level
cross-decade comparisons cautiously and prefer county/MSA aggregation
when the question tolerates coarser geography.

32 unit tests covering cutover dates, schema, all three NA modes
including a 3-contributor merge for the renormalize partial-loss case,
median treatments, and real-data Lake County population preservation.

### `scripts/build_tract_boundary_crosswalk.py` (new)

Downloads `tab20_tract20_tract10_natl.txt` (~18 MB pipe-delimited UTF-8
with BOM, 126,450 rows nationally), parses, computes both weight
orientations, classifies `relationship_type` per row (unchanged / split
/ merge / partial), writes a 4.80 MB snappy parquet to `build/`.

National coverage:
- 85,528 unique 2020 tracts
- 74,134 unique 2010 tracts
- relationship breakdown: 32,177 unchanged / 25,078 split / 9,669 merge / 59,526 partial
- 480 weight cells (0.38%) NA from water-only tract intersections

Pandas metadata stamps build_date, source_url, total_rows, weight_basis="area".

### Distribution: GitHub release asset under separate tag

Per session 3's pattern but with a distinct release tag
(`boundaries-v1`, env-overridable via
`TELLTALERE_CENSUS_BOUNDARY_RELEASE_TAG`). Census re-releases the
relationship file once per decade, so the crosswalk version cadence
diverges from the package version.

The 4.80 MB parquet does NOT ship in the wheel. Wheel size unchanged
from session 4 (~776 KB).

### End-to-end integration test (`tests/test_e2e_lake_county_trends.py`)

Seven tests:
1. Multi-vintage long-format shape sanity (Illinois 2018 + 2023 5-year)
2. Boundary cutover lookup matches docs
3. Population trend on a tract that's `unchanged` across the cutover
   (TrendResult fields populated, MOE-combined matches hand calc)
4. Renter-share significance test on raw counts
5. Lake County 2018 → 2020 harmonization preserves total population
   within 2% tolerance (cross-county bleed-through is the residual)
6. Median rent column → NaN under default `median_treatment="nan"`
7. Cumulative trend on synthetic 5-vintage series produces N-1
   consecutive-pair results in correct year order

Cached ACS5 fetch makes subsequent runs <2s. PUMS endpoint outage does
not affect this test.

## Public surface re-exports

`telltalere_census/__init__.py` extended with 11 new exports:
- `fetch_acs_data_multi_vintage`, `get_available_vintages`
- `compute_trend_metrics`, `cagr`, `is_change_significant`,
  `cumulative_trend`, `TrendResult`
- `get_boundary_vintage_for_acs`, `download_tract_boundary_crosswalk`,
  `load_tract_boundary_crosswalk`, `harmonize_to_2020_boundaries`

Total public surface: **36 names** (up from 25 in session 4).

## What did NOT change

- **Session 3 byte-identity gates** still green (10/10 plus 1 live-PUMS
  auto-skip).
- **Session 4 e2e + unit tests** still green (synthetic-PUMS variants
  pass; live-PUMS auto-skip continues).
- **Marion benchmark** still green (8/8).
- **Core_BTR is unmodified.** `git status` in `C:\Users\ndste\Core_BTR`
  shows zero source changes introduced by this session.

## Census PUMS API outage (continuing from sessions 3 and 4)

Session 5 housekeeping retry of
`scripts/generate_lake_county_fixtures.py` failed again with HTTP 500.
The PUMS endpoint outage now spans four sessions. ACS5 summary tables
are healthy and unaffected. PUMS-conditional tests (3 from session 4 +
1 from session 3) auto-skip; activation pattern unchanged from session 4.

Session 5's three new modules don't depend on PUMS, so the outage is
not blocking.

## Things deferred to future sessions

1. **True population weights for boundary harmonization.** Join 2020
   Census P.L. 94-171 block-level population data to the intersection
   geometries in the build script. Eliminates the area-uniform-
   population assumption, fixing the greenfield-bias and zoning-split
   failure modes. Significant scope expansion (~2-3 hours). Defer to
   a dedicated session.
2. **Multi-vintage PUMS support.** PUMS variable definitions drift
   across vintages and the file format is structurally different from
   ACS summary tables. Out of scope for this session.
3. **CPI-adjusted real-dollar income comparison.** Apply BLS CPI-U-RS
   series to bracket boundaries / median variables before trend
   computation. Out of scope for v0.3.0; document the gotcha so
   consumers know to apply at their layer.
4. **Variable concept registry for vintage-stable code mapping.** The
   starter trend variable set has stable codes, so this isn't needed
   yet — flag as future work.
5. **Block-group level historical trends.** BG boundaries also
   changed between 2010 and 2020 with much higher fragmentation than
   tracts; tract-level is the practical floor for v1. Future session.
6. **Decadal median harmonization.** Mathematically not meaningful via
   weighted allocation. Consumers should aggregate to county/MSA scope
   for decadal median trends.
7. **B25034 year-built granularity.** Defaults include only buckets
   `_001E.._004E`; pre-2010 era detail (`_005E.._011E`) not included.
   Expand when a consumer needs finer building-era trend granularity.

## Stopping conditions encountered

- **Census PUMS endpoint outage** — same as sessions 3 and 4. Did not
  block any session-5 work; new modules don't depend on PUMS.
- **Boundary file size budget checkpoint** — built parquet was 4.80 MB,
  under the 5 MB threshold for in-wheel shipping but per user direction
  shipped via runtime download anyway (mirrors session 3's tract
  polygon pattern).
- **Column-naming deviation from brief** — used `area_weight_*` instead
  of brief's spec'd `population_weight_*` because the underlying data
  is land-area intersections, not population. Approved.
- All other stopping conditions cleared:
  - byte-identity gates from session 3 still green
  - Marion benchmark still green
  - session 4 e2e green
  - end-to-end trend integration test passes every assertion
  - wheel build untouched (no new bulk data shipped this session;
    crosswalk is a release asset)
