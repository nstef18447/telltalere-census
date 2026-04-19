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
| `bg_fetch` | Census ACS5 block-group summary fetch, variable-list driven, parquet caching |
| `county` | County name / FIPS resolution via Census geography API |
| `puma_crosswalk` | PUMA ↔ county crosswalk + population-weighted allocation |
| `cbsa_crosswalk` | County ↔ CBSA crosswalk |
| `weights` | Weight-aware aggregation helpers (weighted sums, weighted averages, groupby-weighted) |
| `derived_fields` | Domain-neutral derived columns: cost burden, age cohort, tenure, structure type, building era, bedroom tier, household type, MV-based buckets, education, employment, poverty flags, etc. |
| `variables` | PUMS / ACS5 variable code constants, whitelists, and bucketing lookups |
| `cache` | Parquet cache conventions (filename scheme, vintage handling) |

## Variable whitelist pattern

Both `pums_fetch.fetch_pums_data()` and `bg_fetch.fetch_bg_data()` take a list of
variable codes as an argument. They do not hardcode which variables to fetch.
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
- `tract_pop_2020_{state_fips}.parquet`

Cache directory defaults to `./data/` relative to the process CWD. Override by
passing `cache_dir=Path(...)` to fetch functions, or by setting the
`TELLTALERE_CENSUS_CACHE_DIR` env var.

To invalidate a cached file, delete it from disk and re-run.

## API keys

`CENSUS_API_KEY` — optional. Recommended for >500 queries/day. Read lazily by
fetch functions; never validated at import time.

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

## Testing

```bash
pytest tests/
```

The Marion County benchmark (`tests/test_marion_benchmark.py`) is the
regression gate. It compares post-extraction results against a baseline
captured from the pre-extraction ami-tool pipeline for Marion IN (FIPS 18097,
2024 ACS5) — exact equality on weighted totals, per-PUMA breakdowns, and
derived field distributions.
