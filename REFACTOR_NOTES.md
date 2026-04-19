# Refactor Notes — ami-tool → telltalere-census extraction

One-time refactor that lifted the Census data layer out of `ami-tool` into a
standalone package consumed via editable install.

Date captured: 2026-04-19.

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
| `telltalere_census/bg_fetch.py` | ACS5 block-group summary fetch, variable-list driven. Parallel structure to `pums_fetch`. Required by the BTR product. |
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

3. **BG cache keyed on `(year, state)` not `(year, state, vars)`**.
   `bg_fetch.fetch_bg_data` caches to a single parquet per state/year.
   Callers requesting different variable sets will either hit a cache miss
   or retrieve the cached file and find their requested columns missing.
   The current logic detects missing columns and re-fetches, but this
   thrashes the cache. Proper fix is either per-varlist-hash cache files
   or union-of-columns merging. Deferred to when a second consumer
   surfaces the problem.

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
