# Refactor Notes — ami-tool → telltalere-census extraction

One-time refactor that lifted the Census data layer out of `ami-tool` into a
standalone package consumed via editable install.

Date captured: 2026-04-19 (session 1).
Updated: 2026-04-19 (session 2 — `bg_fetch` replaced by `acs_fetch`).
Updated: 2026-05-03 (session 3 — Core_BTR primitives port).
Updated: 2026-05-04 (session 4 — owner side, $150k+ tail, lifestage).
Updated: 2026-05-04 (session 5 — multi-vintage, trends, boundary harmonization).

## Session 5 — Multi-vintage, trends, boundary harmonization (2026-05-04)

Adds the time dimension to the package. Three new modules + a runtime-
download GitHub-release asset for the boundary crosswalk. Full session
details in `docs/sessions/5_multi_vintage_trends_boundaries.md`.

| Module / file | Change |
| --- | --- |
| `variables.py` | `B01003_001E` (total population) added to defaults. 85 → 86 estimate vars; MOE auto-pairing brings total to 172 (4 chunks at 50/50/50/22). |
| `multi_vintage.py` (new) | `fetch_acs_data_multi_vintage`, `get_available_vintages`. Thin wrapper over single-vintage path; vintages validated before fetch. |
| `trends.py` (new) | `compute_trend_metrics`, `cagr`, `is_change_significant`, `cumulative_trend`, `TrendResult`. Pure-function; MOE-aware significance with both Census-recommended combined-MOE and CI-overlap methods. |
| `boundaries.py` (new) | `get_boundary_vintage_for_acs`, `download/load_tract_boundary_crosswalk`, `harmonize_to_2020_boundaries`. Three NA modes (propagate / zero / renormalize). |
| `scripts/build_tract_boundary_crosswalk.py` (new) | Downloads Census's 18 MB national pipe-delimited tract relationship file; outputs 4.80 MB snappy parquet (126,450 rows, 85,528 unique 2020 tracts) to `build/`. |
| `tests/test_multi_vintage.py` (new, 18 tests) | Mocked single-vintage path, vintage validation, ACS1/ACS5 availability. |
| `tests/test_trends.py` (new, 36 tests) | Hand-computable cases, edge cases, combined-vs-overlap divergence, all TrendResult fields. |
| `tests/test_boundaries.py` (new, 32 tests) | Cutover dates, schema, all three NA modes incl. 3-contributor renormalize, real-data Lake County preservation. |
| `tests/test_e2e_lake_county_trends.py` (new, 7 tests) | Real 2018 + 2023 5-year fetch, trend on stable tract, MOE significance, harmonization round-trip. |
| `__init__.py` | 11 new public exports. Total public surface: 36 names. |

**Honest naming on weights.** The boundary crosswalk uses `area_weight_2010` /
`area_weight_2020` rather than the brief's spec'd `population_weight_*`.
Reason: Census's relationship file (`tab20_tract20_tract10_natl.txt`)
contains AREALAND intersection areas, not population. Population-
weighted allocation would require joining 2020 P.L. 94-171 block
populations to the intersection geometries; significant scope expansion
documented as future work. The area-weight approximation is standard
in tract-harmonization research and accurate when populations are
roughly uniform within boundary changes.

**Failure mode caveat documented prominently.** Area-weighting
systematically *understates* growth in greenfield-development tracts
(Sun Belt corridors, exurban expansion, brownfield-to-residential)
where the 2010 source-tract population was concentrated only in the
already-developed portion. It also fails on tracts split along
zoning/physical boundaries (residential vs. non-residential halves).
Failure is asymmetric and direction-dependent. Documented in
`harmonize_to_2020_boundaries` docstring under "When this assumption
breaks". Consumers in growth markets should treat tract-level cross-
decade comparisons cautiously and prefer county/MSA aggregation when
the question tolerates coarser geography.

**Boundary crosswalk distribution.** Mirrors session 3's tract-polygon
runtime-download pattern. Single 4.80 MB national parquet, NOT in
wheel; uploaded as a GitHub release asset under a separate tag
(`boundaries-v1`, decoupled from package version since Census re-
releases the crosswalk approximately once per decade). Cache resolves
via `TELLTALERE_CENSUS_BOUNDARY_CACHE` env var or
`platformdirs.user_cache_dir/'boundaries'`.

**Tract / BG boundary cutover (verified against
geography-boundaries-by-year.{YYYY}.html for 2019 / 2020 / 2021 / 2022 /
2023 / 2024 on 2026-05-04):**
- ACS5: end-year ≤ 2019 uses 2010 boundaries; end-year ≥ 2020 uses 2020.
- ACS1: end-year ≤ 2019 uses 2010 boundaries; end-year ≥ 2021 uses 2020
  (2020 1-year not published).
- `get_boundary_vintage_for_acs` is scoped to tract/BG only. PUMAs,
  Congressional Districts, and Urban Areas migrated on the 2022 1-year
  schedule per Census user note 2023-02 — out of scope for this
  function.

**Inflation-comparability is documented, not solved.** Income-bracket
and dollar-denominated variables across vintages are vintage-native
nominal dollars. Documented prominently in `multi_vintage` and
`trends` module docstrings + the README. Real-dollar harmonization
explicitly out of scope as of v0.3.0; consumers apply CPI deflator at
their own layer. Documented as future scope.

**Census PUMS endpoint outage continuing from sessions 3 and 4.**
Session 5 housekeeping retry (`scripts/generate_lake_county_fixtures.py`)
also failed with HTTP 500. PUMS-conditional tests remain skipped (3 from
session 4 + 1 from session 3). Session 5's new modules don't depend on
PUMS — ACS5 summary tables are healthy.

**Marion benchmark and session 3/4 byte-identity gates green.**
54 regression tests pass. New session 5 tests: 36 + 18 + 32 + 7 = 93.
Total suite: 147 passing, 3 skipped.

### Carry-overs deferred to future sessions

- **True population weights for boundary harmonization.** Join 2020
  Census P.L. 94-171 block-level population data to the intersection
  geometries in the build script. Eliminates the area-uniform-
  population assumption. Significant scope expansion (~2-3 hours).
- **Multi-vintage PUMS support.** PUMS variable definitions drift
  across vintages; structurally different from ACS summary tables.
- **CPI-adjusted real-dollar income comparison.** Apply BLS CPI-U-RS
  series to bracket boundaries / median variables before trend
  computation.
- **B25034 year-built granularity.** Defaults include only the first
  4 buckets (`_001E.._004E`); pre-2010 era detail (`_005E.._011E`) not
  included. Out of session 5 scope; expand when a consumer needs
  building-era trend at finer resolution.

## Session 4 — Owner support, tail decomposition, lifestage rollup (2026-05-04)

Extends session 3's tract-synthesis primitives with owner ACS variables,
PUMS-driven $150k+ tail decomposition, and a configurable lifestage
rollup. Full session details in `docs/sessions/4_owner_tail_lifestage.md`.

| Module / file | Change |
| --- | --- |
| `variables.py` | `ACS_BG_DEFAULT_VARS` extended 45 → 85 estimate vars: owner B25118 (`_002E`, `_003E.._013E`), owner B25007 (`_002E`, `_003E.._011E`), full B25009 table including the renter brackets that previously lived only in Core_BTR's pipeline. |
| `income_tail.py` (new) | `decompose_high_income_tail`, `apply_tail_decomposition`, `apply_tail_decomposition_to_crosstab`, `DEFAULT_HIGH_INCOME_SUB_BRACKETS`, `TailDecomposition`. Tenure-agnostic; per-other-column rounding policy preserves the rake's column-sum invariant. |
| `lifestage.py` (new) | `LifestageGrid`, `RCLCO_DEFAULT_LIFESTAGE_GRID`, `aggregate_to_lifestages` (KeyError on unmapped cell), `aggregate_to_lifestages_with_tail` (roll_up + split modes). |
| `tests/_btr_configs.py` (new) | Test-internal renter + owner BracketConfigs and scalar PUMS binners. NOT a public surface. |
| `tests/test_e2e_lake_county_demand.py` (new) | End-to-end integration with synthetic-PUMS (always runs) + live-PUMS (auto-skip) variants. Asserts marginal-sum, column-sum, tail-preservation, and lifestage-total invariants. |
| `tests/test_income_tail.py` + `tests/test_lifestage.py` (new) | 36 unit tests across the two new modules. |
| `__init__.py` | New public surface re-exported. |
| `scripts/generate_lake_county_fixtures.py` | Drop now-redundant `B25009_RENTER_VARS` append. Document tenure model (single tract + single PUMS fixture, consumers filter via TEN). |

**Session 3 byte-identity gate preserved.** Deliberately did not retrofit
`authoritative_total_var` onto `compute_tract_marginal` (which my session
3 implementation does not accept; the session 4 spec language was
outdated relative to session 3's shipped surface). The e2e test uses
`read_authoritative_total(row, "B25003_002E")` as a separate invariant
check, not as a function argument. Marion benchmark + 10 byte-identity
parity tests remain green; both auto-skipped live-PUMS gates also still
auto-skipped (Census PUMS endpoint outage continues).

**ACS cache invalidation cost.** Adding owner brackets + B25009 to
defaults is a one-time merge-union re-fetch per cold state in any
consumer's runtime cache (~3-4 minutes per state). Documented in the
session doc. Verified end-to-end on Lake County: 4-chunk fetch at 170
vars total, 160 tract rows captured, both cross-table invariants exact
(`B25003_002 + _003 == _001` and `sum(owner B25118 brackets) == _002E`).

**`tests/_btr_configs.py` rationale.** Hand-constructed test-only
helper rather than a `telltalere_census.testing.btr_fixtures` public
submodule. Three reasons (committed verbatim from the session 4
read-back): (a) BTR-flavored configs would leak BTR concepts into the
package boundary even under a `testing/` subpath, (b) we have one
consumer using this scheme today and one prospective (the demographics
inspector) — until a second consumer actually needs the same configs,
we don't know which slice generalizes, (c) the parity test already
imports BTR's configs directly via `sys.path` for byte-identity
verification, and that pattern works. Revisit if the inspector exposes
real second-consumer requirements.

**Census PUMS API outage continuing from session 3.** All four
PUMS-conditional tests in the suite (3 from session 4, 1 from session
3) auto-skip. Re-running `scripts/generate_lake_county_fixtures.py`
once the endpoint recovers re-activates all four. Synthetic e2e test
passes every invariant in the meantime.

## Session 3 — Core_BTR primitives port (2026-05-03)

Lifted six generic Census-processing primitives out of `Core_BTR` into
`telltalere-census` so other consumers (a near-term demographics
inspector frontend, future client engagements) can use them without
depending on Core_BTR. **Core_BTR was not modified** — `git status` in
`C:\Users\ndste\Core_BTR` shows zero source changes after this session.
Pre-existing dirty files (`SHELL_PROMPT.md`, batch outputs) were
untouched by this session and remain in their prior state.

Full session details in `docs/sessions/3_port_primitives.md`. Summary
of what shipped:

| Module | Purpose | Bytes-identical with Core_BTR? |
| --- | --- | --- |
| `binning.py` | `bin_acs_row` mechanism | yes (verbatim) |
| `bracket_allocation.py` | `proportional_from_brackets` | yes (verbatim) |
| `tract_marginals.py` | `compute_tract_marginal`, `subtotal_moe_flagged` | yes (160 Lake County tracts × 3 dims) |
| `ipf.py` | `rake_to_marginals` | yes (synthetic + 50 real Lake County marginals) |
| `puma_crosstab.py` | `compute_puma_joint`, `joint_to_column_major`, `column_major_to_joint` | yes (synthetic PUMS) |
| `geometry.py` | tract polygon + crosswalk loaders, downloader | new — no parity reference |

Plus two build scripts (`scripts/build_tract_puma_crosswalk.py`,
`scripts/build_tract_polygons.py`), the in-wheel crosswalk parquet
(`telltalere_census/data/tract_to_puma_2020.parquet` — 861 KB),
fixture generator (`scripts/generate_lake_county_fixtures.py`), and
the parity test (`tests/test_core_btr_parity.py`).

**Core_BTR/package duplication.** Both repos now contain implementations
of the same logic (Core_BTR's `_bin_acs_row` and the package's
`bin_acs_row`, etc.). Core_BTR is intentionally unchanged; collapsing
the duplication into a single source of truth is a future cleanup once
Core_BTR's BTR-specific consumers can switch to the package without
risk to its active client engagement.

**Census PUMS API outage** during this session blocked capture of a
live Lake County PUMS fixture for the byte-identity parity test. The
test (`test_puma_crosstab_byte_identical_live_pums`) is wired and
auto-activates when `tests/fixtures/lake_county_pums.parquet` appears.
Tract-level live data was captured (ACS5 endpoint was healthy) and
the tract-level parity tests cover all 160 Lake County tracts × 3
dimensions byte-identically.

**`compute_puma_joint` opt-in zero-fill (`bin_orders` parameter)**.
By default `compute_puma_joint` emits only observed (dim, dim) cells —
matches Core_BTR's `_weighted_pivot` pre-adapter shape. The IPF flow
zero-fills downstream via `joint_to_column_major`. With `bin_orders`
the result is reindexed to the cartesian product of declared bin
orders, useful for consumers that want a complete DataFrame without
going through the IPF adapter.

**Geometry distribution**: tract polygons do NOT ship in the wheel
(would push it past 25 MB even after `shapely.simplify(0.0005)`). Per-
state parquets are built by `scripts/build_tract_polygons.py` to
`build/`, then uploaded as GitHub release assets. The runtime helper
`download_tract_polygons` fetches them on demand into a
platformdirs-based user cache. Wheel stays small (~1 MB total package
data: just the crosswalk + the existing `puma_county_weights.parquet`).

Release URL configuration is parameterized via env vars
(`TELLTALERE_CENSUS_RELEASE_OWNER`, `TELLTALERE_CENSUS_RELEASE_TAG`),
default placeholders. Operator sets these once after publishing the
release.

## Session 2 — `acs_fetch` implementation (2026-04-19)

Session 1 shipped `bg_fetch.py` as a minimal state-level fetcher keyed on
`(year, state)`. Session 2 replaced it with `acs_fetch.py`, expanding
scope and fixing a latent bug.

**Renamed** `bg_fetch.py` → `acs_fetch.py`. The new name pairs with
`pums_fetch.py` (both named by the Census product they pull from) and
reflects that the module now supports both block-group and tract
geography in one code path.

**Fixed** a latent API bug: `bg_fetch` used `in=state:{st}` for
block-group queries, which returns `400 unknown/unsupported geography
hierarchy` against 2024 ACS5. Block-group queries require
`in=state:{st} county:*`. Verified by direct API probe on 2026-04-19.
Tract queries still work with `in=state:{st}`. Both patterns documented
in `acs_fetch.py`'s module docstring with the test date.

**Added** explicit MOE auto-pairing: when `include_moe=True` (default),
every `_E` estimate variable is paired with its `_M` companion inside
the fetcher. Callers never type MOEs.

**Added** chunking for the Census API's 50-variable-per-call limit.
`fetch_acs_data` sorts its variable list, splits into deterministic
≤50-var chunks, fetches each chunk independently, and inner-joins on
the composite GEOID. Chunk-boundary determinism matters for cache
repeatability and debugging. Row-count mismatches across chunks log a
warning (not silent outer-join).

**Added** special-value normalization. Census sentinels
(`-555555555`, `-222222222`, `-333333333`, `-666666666`, `-888888888`,
`-999999999`, plus string sentinels `""`, `"*"`, `"**"`, `"***"`,
`"(X)"`, `"null"`, `"N"`, `"-"`) are mapped to pandas NA. Numeric
columns use nullable `Int64` (whole numbers) or `Float64` (fractional,
e.g. average household size). `.isna()` works uniformly downstream.

**Added** tract geography support: `acs5_{vintage}_{state_fips}_tract.parquet`
alongside the block-group parquet. Same caching semantics; same merge-union
behavior on variable-list misses.

**Added** 45-variable BTR-oriented default list (`ACS_BG_DEFAULT_VARS`),
replacing the 6-variable starter set. Covers tenure totals, renter
income distribution (B25118), renter-age distribution (B25007), rent
paid (B25064/B25071), median home value (B25077/B25088), units in
structure (B25024), and year built (B25034). MOE auto-pairing brings
the request to 90 codes (2 chunks under the 50-var API limit).

**Verification caught two offset errors in the session 2 spec**
before writing any fetch code:
  - Renter income brackets (B25118): spec had `_014E..024E`; actual is
    `_015E..025E` (spec confused the `_014E` renter subtotal with the
    `< $5,000` bracket).
  - Renter age brackets (B25007): spec had `_009E..017E`; actual is
    `_013E..021E` (spec's `_009E` is "owner 65-74 years").
Corrected codes verified via `api.census.gov/data/2024/acs/acs5/variables/{var}.json`.
Both renter subtotals (B25118_014E, B25007_012E) included as
sanity-check variables to enable `sum(brackets) ≈ subtotal` assertions.

**Added** `tests/test_acs_fetch_lake_county.py` — pytest integration
suite marked `@pytest.mark.integration`. Fetches Illinois at both
block-group (`len(lake) >= 300`) and tract (`len(lake) >= 80`)
geography, asserts total households within 5% of authoritative Lake
County 2024 ACS5 value (258,455), renter share 20-40%, MOE columns
complete, median gross rent coverage ≥50% (actual ~60% at BG; Census
suppresses this field with `-666666666` in ~40% of Lake County BGs due
to sample-size thresholds). 12/12 tests pass. Marion benchmark also
passes unchanged as smoke check.

**Added** pytest marker registration to `pyproject.toml` under
`[tool.pytest.ini_options]`.

### Things session 2 deferred

1. **Per-varlist cache filename hashing**. `acs_fetch` still caches per
   `(vintage, state, geography)`, not `(vintage, state, geography,
   sorted_variable_tuple)`. When a caller requests a new variable, the
   fetcher re-fetches the UNION and rewrites the parquet. This is
   correct but thrashes the cache if two callers alternate disjoint
   variable sets. Deferred to when that access pattern appears.

2. **Vintage-aware `in=` clause**. Currently hardcoded per geography
   (BG: `state:X county:*`, tract: `state:X`). If a future vintage
   changes this, branch on `vintage` in `_fetch_one_chunk`.

3. **Non-standard variable codes** (`NAME`, `GEO_ID`). Currently
   unsupported — `_moe_of` returns None for codes not ending in `E`.
   If a caller needs these, extend the fetcher (they don't have MOE
   companions; just pass through).

## What moved where

| ami-tool source | → telltalere-census destination | Notes |
| --- | --- | --- |
| `config.py` → state FIPS dicts, `CROSSWALK_URL`, `H_VARS`/`P_VARS`, `BLD_TO_STRUCTURE`, `BUILDING_ERA_BINS`, `HHT_TO_TYPE`, `AGE_COHORT_BINS`, `BEDROOM_TIERS` | `telltalere_census/variables.py` | `H_VARS`/`P_VARS` renamed to `PUMS_DEFAULT_HOUSING_VARS` / `PUMS_DEFAULT_PERSON_VARS` in the shared package. ami-tool's `config.py` re-exports them under the old names for backward compatibility. |
| `data_fetch.py` → `_fetch_records`, `_fetch_h_records`, `_fetch_p_records`, `_apply_puma_weights`, `fetch_pums_data`, `_build_puma_var` | `telltalere_census/pums_fetch.py` | Pure behavior extract. Now takes optional `housing_vars`, `person_vars`, `cache_dir` kwargs — defaults reproduce the old behavior exactly. |
| `data_fetch.py` → `resolve_county` | `telltalere_census/county.py` | Domain-neutral county resolver. Reads `CENSUS_API_KEY` from env lazily at call time (previously read at config.py import time). |
| `puma_crosswalk.py` → entire file | `telltalere_census/puma_crosswalk.py` | Module-level `_last_county_fips` global preserved. See coupling flags. |
| `derived_fields.py` → `tenure` mapping, cost burden, age cohort, building era, structure type, bedroom tier, household type, MV-based buckets, mover type, has_children, vehicle access, employment status, education level, snap, broadband, poverty flag | `telltalere_census/derived_fields.py` (`add_all`) | AMI-dependent fields (`choice_renter`, `affordability_trap`, `below_market_indicator`) stayed in ami-tool's `derived_fields.compute_all_derived`, which now calls the shared `add_all` first, then layers AMI-specific logic. |
| `data/puma_county_weights.parquet` | `telltalere_census/data/puma_county_weights.parquet` | Shipped as package data. ami-tool's copy in `ami-tool/data/` preserved (not deleted per refactor rules). |
| `data/county_to_cbsa.parquet` | `telltalere_census/data/county_to_cbsa.parquet` | Shipped as package data. |

## New modules (not present in ami-tool before)

| New file | Purpose |
| --- | --- |
| `telltalere_census/acs_fetch.py` | ACS5 block-group / tract summary fetch, variable-list driven, MOE auto-pairing, chunked for 50-var API limit. Parallel structure to `pums_fetch`. Required by the BTR product. (Originally shipped in session 1 as `bg_fetch.py` — replaced in session 2, see below.) |
| `telltalere_census/cbsa_crosswalk.py` | Thin loader for the shipped `county_to_cbsa.parquet`. Does not build the crosswalk — that logic still lives in ami-tool's `costar_fetch.py`. |
| `telltalere_census/cache.py` | Filename convention helpers (`pums_h_filename`, `bg_acs5_filename`, etc.) plus `resolve_cache_dir()`. Resolution order: explicit arg → `TELLTALERE_CENSUS_CACHE_DIR` env var → `./data/` relative to CWD. |
| `telltalere_census/weights.py` | Weight-aware aggregation helpers: `weighted_sum`, `weighted_count_by`, `weighted_mean`, `weighted_share`. Consolidates the `sum(WGTP)` / `np.average(..., weights=WGTP)` patterns previously inlined across ami-tool. |
| `tests/test_marion_benchmark.py` | Pytest regression suite — 8 metrics against frozen fixture at `tests/marion_baseline.json`. |

## What stayed in ami-tool (product-specific)

- `data_fetch.fetch_hud_income_limits` — HUD API, requires HUD_API_KEY
- `ami_logic.py` — AMI threshold extrapolation, bucket assignment, `tenure` mapping is duplicated here (redundant with shared `derived_fields.add_all`; both produce identical output)
- `derived_fields.compute_all_derived` — wrapper that calls shared `add_all`, then layers AMI-specific fields
- `lenses.py`, `scoring.py`, `exhibits.py`, `export_excel.py`, `generate_thesis.py` — all AMI-specific output
- `costar_fetch.py`, `zillow_fetch.py`, `zillow_metrics.py`, `permits_fetch.py`, `hud_subsidized_fetch.py`, `supply_metrics.py`, `puma_metrics.py` — product-specific data sources
- `config.py` — HUD_API_KEY validation, AMI band definitions, `DISPLAY_BANDS`, `BUCKET_TO_BAND`, `HH_SIZE_FACTORS`. Also sets `TELLTALERE_CENSUS_CACHE_DIR=DATA_DIR` at import time so the shared fetchers reuse ami-tool's existing 5GB state PUMS cache.

## Coupling flags (priority follow-ups)

### 1. `_last_county_fips` module-level global

**Location**: `telltalere_census/puma_crosswalk.py`

**What**: `get_pumas_for_county` writes the current county FIPS into a
module-level variable, which `pums_fetch.fetch_pums_data` reads out to
decide whether to apply PUMA-county population weights for shared PUMAs.

**Why this matters**: any process that interleaves two calls to
`get_pumas_for_county` before calling `fetch_pums_data` will see the
second county's FIPS when applying weights for the first. This is a
reentrancy and thread-safety hazard. For single-county processing it is
fine; for parallel processing it is wrong.

**Action**: preserve for now (ami-tool is serial today), but fix before
any downstream product processes multiple counties in parallel. Suggested
fix: thread the county FIPS through `fetch_pums_data`'s signature as a
keyword argument, or return a small `PumaSelection` dataclass from
`get_pumas_for_county` that the caller passes forward.

### 2. HUD_API_KEY module-level `sys.exit`

**Location**: `ami-tool/config.py`

**What**: importing `ami-tool/config.py` calls `sys.exit` immediately if
`HUD_API_KEY` is missing from the environment.

**Why this matters**: any future ami-tool code that imports anything from
`telltalere_census` must NOT depend on HUD env vars. The shared package
does not import `config.py` — this constraint is preserved today because
`config.py` is imported only by product-specific modules. Violating it
would make the shared package impossible to use from `Core_BTR` or future
products that have no HUD dependency.

**Action**: leave as-is. This is a product-local rule, not a code change
request. Noting it so future extraction or refactor sessions don't
silently introduce an import path that pulls HUD logic into the shared
package.

## Behavior-preserving simplifications

None. This was a pure move — no logic was "improved" in flight.

Two cosmetic differences:

1. **Cache directory resolution**: ami-tool's PUMS fetch used
   `config.DATA_DIR` (anchored to config.py's parent dir). The shared
   fetchers use `resolve_cache_dir()` which defaults to `./data/`
   relative to CWD, overridable via `TELLTALERE_CENSUS_CACHE_DIR` env
   var. ami-tool's `config.py` now sets that env var on import so the
   behavior is identical regardless of process CWD.

2. **`tenure` column is set twice**: `ami_logic.assign_ami_buckets` sets
   `df["tenure"]` from `TEN`, and `derived_fields.add_all` sets it the
   same way. Both produce identical string values ("Owner"/"Renter"), so
   the behavior is unchanged. This duplication should collapse once
   `ami_logic.assign_ami_buckets` drops its own tenure mapping — flagged
   as a tidy-up follow-up, not part of this refactor.

## Things that smelled wrong but were left alone

1. **`sys.exit()` inside library code**. `county.resolve_county` and
   `pums_fetch._fetch_h_records` / `_fetch_p_records` call `sys.exit()` on
   bad input. That's wrong for a library — should raise exceptions. Left
   as-is for behavior preservation.

2. **Global state in `_last_county_fips`** — see coupling flag #1.

3. **ACS cache keyed on `(vintage, state, geography)` not `(vintage,
   state, geography, vars)`**. `acs_fetch.fetch_acs_data` caches to a
   single parquet per state/vintage/geography. Session 2 made the
   missing-variable behavior explicit: re-fetch the UNION of cached and
   newly requested variables and write back as one coherent snapshot
   (not partial deltas). This is correct but thrashes the cache if two
   callers alternate disjoint variable sets. Proper fix is per-varlist-
   hash cache filenames. Deferred to when that access pattern appears.

4. **CBSA crosswalk build logic still lives in `costar_fetch.py`**. The
   lookup half was extracted, but the Census XLSX parsing stays in
   ami-tool. Move it to `telltalere_census/cbsa_crosswalk.py` when a
   second consumer needs to rebuild the crosswalk.

5. **Tract-to-PUMA crosswalk CSV is not shipped** — it's fetched from
   Census on first call and cached. Consider shipping it as package data
   for offline installs.

6. **`puma_metrics.py` lives in ami-tool** despite being mostly
   visualization-neutral. Candidate for extraction to the shared package
   when BTR or another product needs per-PUMA renter metrics.

## How to run the Marion benchmark

```bash
cd /c/Users/ndste/telltalere-census
python -m pytest tests/test_marion_benchmark.py -v
```

The test pins the cache dir to `../ami-tool/data/` (where the
pre-refactor state-18 PUMS parquets already live) so it doesn't hit the
Census API. If the ami-tool cache is absent, the test will fetch fresh
from Census on first run (requires network; optionally set
`CENSUS_API_KEY`).

To **re-capture** the baseline fixture from scratch, delete
`tests/marion_baseline.json` and run:

```bash
cd /c/Users/ndste/ami-tool
python capture_marion_baseline.py
```

The script detects a missing fixture and writes one. **Review the diff
carefully** before committing — any change to the baseline numbers is a
claim that the pipeline's correct output has moved, and needs
justification.

## Install mechanics

From a product repo:

```
-e ../telltalere-census
```

last line of `requirements.txt`, then `pip install -r requirements.txt`.

For ami-tool today:

```bash
cd /c/Users/ndste/ami-tool
python -m pip install -r requirements.txt
```

Verified editable install works on Python 3.14 / pip 25.3.
