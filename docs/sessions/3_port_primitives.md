# Session 3 — Port generic Census primitives from Core_BTR

Status: **complete**.

## Goal

Lift the generic Census-processing primitives out of `Core_BTR` into
`telltalere-census` so other consumers (a near-term demographics
inspector frontend, future client engagements) can use them without
depending on Core_BTR. Core_BTR is unchanged; the package now contains
parallel implementations that produce **byte-identical output** to
Core_BTR's reference code.

## Design intent

All new primitives are **tenure-agnostic and bin-agnostic**. The
package supplies the mechanism, the consumer supplies the configuration:

- No hardcoded ACS variable codes inside function bodies — codes come
  in via `BracketConfig` arguments or pre-binned column names.
- No hardcoded tenure filter (`TEN ∈ {3, 4}` for renters) — the
  consumer passes a `filter_func` callable.
- No hardcoded display-bin schemes (the BTR `INCOME_BINS`,
  `B25118_RENTER_TO_DISPLAY`, etc.) — those stay in Core_BTR's
  `config/demographic_bins.py`.

The package stays neutral so Core_BTR, the demographics inspector, and
future products can layer their own bin schemes / tenure filters /
ACS table selections on top.

## What shipped

### New modules

| Module | Purpose |
| --- | --- |
| `telltalere_census/binning.py` | `bin_acs_row(row, code_to_bin, bin_ids)` — generic ACS bracket-cell → display-bin rollup. Suppressed cells treated as 0. |
| `telltalere_census/bracket_allocation.py` | `proportional_from_brackets(...)` — proportional reallocation across closed brackets + open-ended top bracket. Returns `float \| None`. |
| `telltalere_census/tract_marginals.py` | `compute_tract_marginal(row, BracketConfig)` (atomic — one dimension per call), `read_authoritative_total`, `subtotal_moe_flagged`. |
| `telltalere_census/ipf.py` | `rake_to_marginals(prior, row_marginal, col_marginal)` — 2-D iterative proportional fitting with deterministic post-rounding reconciliation. |
| `telltalere_census/puma_crosstab.py` | `compute_puma_joint(records, dimensions, filter_func, bin_orders)` — pre-binned PUMS aggregation along arbitrary dimensions. Plus `joint_to_column_major` and `column_major_to_joint` adapters bridging the DataFrame shape and IPF's column-major dict shape. |
| `telltalere_census/geometry.py` | Tract polygon + tract-to-PUMA crosswalk loaders, runtime polygon downloader, `wkb_to_geojson`, optional `load_tract_polygons_as_gdf` (under `[geo]` extra). |

### New scripts

- `scripts/build_tract_puma_crosswalk.py` — downloads the 2020 Census
  tract-to-PUMA mapping, writes it to
  `telltalere_census/data/tract_to_puma_2020.parquet` (~860 KB, ships in
  the wheel).
- `scripts/build_tract_polygons.py` — TIGERweb 1:500k tract polygons,
  per-state, with `shapely.simplify(0.0005)` applied at build time.
  Output goes to `build/tracts_{state_fips}.parquet` (gitignored — these
  are intended as GitHub release assets, not wheel contents).
- `scripts/generate_lake_county_fixtures.py` — Lake County, IL fixture
  generator for the parity test. ACS5 tract fetch + PUMS fetch.

### New package data

- `telltalere_census/data/tract_to_puma_2020.parquet` — 85,452 rows
  across 53 jurisdictions. Shipped in wheel.

### New tests

| File | Tests | Purpose |
| --- | --- | --- |
| `tests/test_binning.py` | 7 | bin_acs_row mechanics |
| `tests/test_bracket_allocation.py` | 11 | proportional reallocation including top-bracket and edge cases |
| `tests/test_tract_marginals.py` | 13 | compute_tract_marginal, read_authoritative_total, subtotal_moe_flagged |
| `tests/test_ipf.py` | 9 | synthetic / hand-computable IPF cases |
| `tests/test_puma_crosstab.py` | 22 | compute_puma_joint, bin_orders zero-fill, both adapters |
| `tests/test_geometry.py` | 16 | crosswalk + polygon loaders, downloader URL composition |
| `tests/test_core_btr_parity.py` | 10 | byte-identity vs Core_BTR's reference implementation |

**Marion benchmark unchanged** — 8/8 still pass.

## Byte-identity parity (the strongest correctness bar)

`tests/test_core_btr_parity.py` imports Core_BTR directly via `sys.path`
and asserts that the package primitives produce output identical to
Core_BTR's reference code:

- **`compute_tract_marginal` vs Core_BTR's `bin_b25{118,007,009}_renter_row`**:
  byte-identical across **all 160 Lake County tract rows × 3 dimensions**
  (income, age, hh_size).
- **`subtotal_moe_flagged` vs Core_BTR's `_subtotal_moe_flagged`**:
  identical flag value across 160 tracts × 3 subtotal codes.
- **`rake_to_marginals` vs Core_BTR's `ipf_2d`**: byte-identical
  joint dict on a synthetic 6×6 prior + manually-built marginals AND on
  50 real Lake County tract marginals.
- **`compute_puma_joint` + `joint_to_column_major` vs Core_BTR's
  `compute_puma_crosstabs`**: byte-identical for income×age, income×hh_size,
  and the three 1-D marginals on a synthetic PUMS-shaped DataFrame.

A live-PUMS parity test (`test_puma_crosstab_byte_identical_live_pums`)
is wired but auto-skipped — the Census PUMS endpoint was throttled (500
errors on every state) during this session's fixture generation. The
test activates automatically when `tests/fixtures/lake_county_pums.parquet`
appears; re-run `scripts/generate_lake_county_fixtures.py` once the
endpoint recovers.

## Key design decisions

- **`ipf.rake_to_marginals` is 2-D only**. Core_BTR's IPF is a matrix
  scaling algorithm; N-D rake is a different algorithm. Documented as
  scope; deferred.
- **`compute_puma_joint` returns a DataFrame, but `rake_to_marginals`
  takes a column-major dict-of-dicts**. Two adapters bridge the shapes
  (`joint_to_column_major`, `column_major_to_joint`). The dict shape
  was preserved on the IPF side specifically to make the byte-identity
  parity test enforceable; future consumers (e.g. session 4 lifestage
  rollup) can use the reverse adapter to get a DataFrame back.
- **`compute_puma_joint(..., bin_orders=...)`** is opt-in zero-fill.
  Without it, only observed combinations are emitted (matches
  Core_BTR's pre-adapter shape; the IPF flow zero-fills downstream
  via `joint_to_column_major`). With it, the result is reindexed to
  the cartesian product of the declared bin orders.
- **Tract polygons do NOT ship in the wheel.** Per-state parquets are
  built by `scripts/build_tract_polygons.py` to `build/`, then uploaded
  as GitHub release assets. The runtime helper
  `download_tract_polygons` fetches them on demand. Wheel stays
  ~1 MB (the in-wheel crosswalk is the only large data file).
- **Geopandas is NOT a hard dependency.** The canonical polygon shape
  is a DataFrame with a `polygon_wkb` bytes column.
  `load_tract_polygons_as_gdf` requires the `[geo]` extra.

## Things deferred to session 4

The session brief explicitly named:

1. **Owner-side support**. The package primitives are tenure-agnostic
   so the next session can layer owner BracketConfigs (B25003_002E,
   B25118 owner section, etc.) without touching package code.
2. **$150k+ income tail decomposition via PUMS**. The closed brackets
   in `proportional_from_brackets` already handle the open-ended top;
   the tail decomposition layers on top of that primitive.
3. **Lifestage rollup primitive**. Will use
   `column_major_to_joint` to feed into a custom rollup that maps the
   IPF synthesized joint into lifestage cohorts. The reverse adapter
   was added in this session specifically to enable that path.

## Carry-overs documented in REFACTOR_NOTES.md

- Core_BTR's `_bin_acs_row`, IPF, compute_puma_crosstabs, etc. continue
  to exist. **Core_BTR is unmodified.** Two implementations of the same
  logic exist temporarily — collapse-to-one is a future cleanup, not in
  scope here. Telltale RE products that depend on Core_BTR's pipeline
  remain stable; new consumers (demographics inspector, future
  engagements) use the package.
- Older carry-over hazards (`_last_county_fips` global,
  `sys.exit()` in library code, ACS cache key) are unchanged. Tracked
  in `REFACTOR_NOTES.md`'s "Things that smelled wrong but were left
  alone" section since session 1.

## Stopping conditions encountered

- **Census PUMS endpoint outage**: returned HTTP 500 throughout the
  session (state 17 + state 18 both failed even on minimal queries;
  ACS5 endpoint was healthy). Tract-level parity tests captured live
  Lake County data and pass. The live-PUMS parity gate is wired and
  auto-activates when the fixture appears on disk.
- All other stopping conditions cleared (parity tests passed; Marion
  benchmark passed; tract polygon parquet sizes within budget; no
  TIGERweb rate-limiting issues; Core_BTR untouched).
