# Session 4 — Owner side, $150k+ tail decomposition, lifestage rollup

Status: **complete**.

## Goal

Extend the session 3 tract-synthesis primitives with three capabilities
that close the gap to a "demographics inspector"-grade demand pipeline:

1. **Owner-side support.** The session 3 primitives are tenure-agnostic
   already; this session ships the ACS variables consumers need to use
   them on owner-occupied data.
2. **$150k+ tail decomposition.** ACS publishes the high-income bracket
   as open-ended; PUMS continuous `HINCP` lets us split it into
   $150-250k / $250-350k / $350-500k / $500k+ at PUMA, then apply
   proportionally to tract crosstabs.
3. **Lifestage rollup.** Configurable (age_bin, income_bin) cell-grid
   that aggregates a crosstab into lifestage labels. Default grid is
   the age-only RCLCO 5-bucket scheme; consumers pass any grid.

## What shipped

### ACS defaults (`telltalere_census/variables.py`)

`ACS_BG_DEFAULT_VARS` extended from 45 → 85 estimate variables:

- **B25118** owner subtotal `_002E` and brackets `_003E.._013E` (11 codes)
- **B25007** owner subtotal `_002E` and brackets `_003E.._011E` (9 codes)
- **B25009** full table — total `_001E`, both subtotals (`_002E` owner /
  `_010E` renter), and both bracket sets (`_003E.._009E` owner,
  `_011E.._017E` renter); previously not in defaults at all
- All renter codes from session 2 / 3 unchanged

Census API call shape goes from 2 chunks of ≤50 to **4 chunks** (50 / 50 /
50 / 20) with MOE auto-pairing.

**Cache invalidation cost:** the existing `acs5_{vintage}_{state}_tract.parquet`
files in any consumer's runtime cache are missing the new owner /
B25009 columns. The next ACS fetch from any consumer (including
Core_BTR if it picks up the new defaults) triggers the documented
merge-union re-fetch (~3-4 minutes per cold state). One-time cost.
The package's existing fixture (`tests/fixtures/lake_county_tracts.parquet`)
was regenerated under the new variable list as part of this session.

Cross-table invariants verified on the regenerated Lake County fixture:
- `B25003_002E + B25003_003E == B25003_001E` exact across all 160 tracts
- `sum(B25118_003E..013E) == B25118_002E` exact across all 160 tracts

Owner cells at 100% coverage on every Lake County tract.

### `telltalere_census/income_tail.py`

Three primitives composing the high-income decomposition:

- **`decompose_high_income_tail(pums, tenure_filter, threshold,
  sub_brackets, weight_col, income_col, min_unweighted_n)`**
  Returns a `TailDecomposition` TypedDict with weighted shares per
  sub-bracket, unweighted record counts, and a `low_sample_flag`.
  Tenure-agnostic (consumer passes filter callable). Empty input
  zero-fills (no NaN propagation).

- **`apply_tail_decomposition(marginal, shares, top_bracket_label)`**
  Rewrites a 1-D tract marginal dict — replaces `top_bracket_label`
  with sub-bracket entries, preserving the integer sum exactly via
  largest-share drift allocation.

- **`apply_tail_decomposition_to_crosstab(crosstab, shares,
  top_bracket_label, income_dim, weight_col)`**
  Same operation on a 2-D long-form crosstab (output of
  `compute_puma_joint` or `column_major_to_joint`), with **per-other-dim
  drift allocation** so the rake's column-sum invariant survives
  unchanged. Documented explicitly in the docstring: cell counts may
  differ by ±1 from `round(global_share * cell)` because drift is
  allocated within each column rather than globally; the column-sum
  invariant is the consumer-facing contract.

`DEFAULT_HIGH_INCOME_SUB_BRACKETS` matches the RCLCO scheme:
`[("150_250k", 150_000, 250_000), ("250_350k", 250_000, 350_000),
("350_500k", 350_000, 500_000), ("500k_plus", 500_000, inf)]`.

20 unit tests + 1 live-PUMS regression test (auto-skipped while the
Census PUMS endpoint is throttled).

### `telltalere_census/lifestage.py`

`LifestageGrid` TypedDict with explicit `cells: dict[tuple[str, str],
str]` mapping. **Safety property:** `aggregate_to_lifestages` raises
`KeyError` (not silently drops) on cells the grid doesn't map. This
catches bin-scheme drift early — adding an income bin to the ACS
rollup but forgetting to extend the lifestage grid fails loudly
instead of silently zeroing data.

`RCLCO_DEFAULT_LIFESTAGE_GRID` — v1 age-only 36-cell grid mapping every
income bin in a given age row to one of:

- `Post-Grad` (under_25)
- `Young Professional` (25_34)
- `Family` (35_44, 45_54)
- `Mature Professional` (55_64)
- `Empty Nester` (65_plus)

`aggregate_to_lifestages_with_tail` handles post-tail-decomposition
crosstabs:
- `tail_treatment="roll_up"` (default): collapses `tail_sub_brackets`
  back into a single `tail_rolled_label` bin before aggregating.
- `tail_treatment="split"`: passes through to
  `aggregate_to_lifestages`, requiring the grid to define cells for
  each sub-bracket explicitly.

16 unit tests covering RCLCO grid sanity, basic rollup, output
ordering, swapped index level order, KeyError on unmapped cell,
ValueError on label-order mismatch, rounding-drift preservation, both
tail-treatment modes, the round-trip identity (decompose + roll_up
equals direct rollup), and four error paths.

**Future config-loader note** (out of scope for v1): tuple-keyed
`LifestageGrid` doesn't round-trip through JSON / YAML. A
`LifestageGrid.from_records([{age, income, label}, ...])` classmethod
can be added when a consumer needs config-file loading.

### End-to-end integration test (`tests/test_e2e_lake_county_demand.py`)

Two tests in one file:

- **`test_e2e_synthetic_pums`** (always runs): real Lake County tract
  fixture + synthesized PUMS (seed=42, n=800, mixed tenure). Exercises
  every primitive end-to-end. Proves the compositional API works.
- **`test_e2e_live_pums`** (auto-skip): activates when the Lake County
  PUMS fixture exists. Same invariants, real-data inputs.

For each `(tract, tenure)` pair the test asserts:
- Marginal sums approximate the authoritative tenure total
  (B25003_002E for owner, B25003_003E for renter)
- Post-rake crosstab column sums equal the age marginal **exactly**
- Tail decomposition preserves each age column's total **exactly**
- Tail shares sum to 1.0 (or 0.0 in the zero-records case)
- Lifestage rollup total equals the raked crosstab total **exactly**

Pipeline composition in the test:

```python
compute_tract_marginal -> read_authoritative_total
  -> compute_puma_joint -> joint_to_column_major
  -> rake_to_marginals -> column_major_to_joint
  -> decompose_high_income_tail -> apply_tail_decomposition_to_crosstab
  -> aggregate_to_lifestages_with_tail (roll_up)
```

### Test-internal helper (`tests/_btr_configs.py`)

BTR-flavored `BracketConfig`s for B25118 / B25007 / B25009 (renter +
owner), plus scalar PUMS binners (`bin_income`, `bin_age`,
`bin_hh_size`). **NOT a public package surface** — lives under
`tests/` specifically so BTR concepts don't leak into the package's
import boundary. Per the session 3 + 4 design intent: bin schemes
belong to consumers, not the package. Decision deferred for the
public API: revisit when a second consumer (the demographics
inspector) actually needs the same configs.

### Public surface re-exports

`telltalere_census/__init__.py` extended with:

- `decompose_high_income_tail`, `apply_tail_decomposition`,
  `apply_tail_decomposition_to_crosstab`,
  `DEFAULT_HIGH_INCOME_SUB_BRACKETS`, `TailDecomposition`
- `aggregate_to_lifestages`, `aggregate_to_lifestages_with_tail`,
  `LifestageGrid`, `RCLCO_DEFAULT_LIFESTAGE_GRID`

## What did NOT change

Per the standing constraint: **session 3 primitives are byte-identity-
gated and were not modified**. `compute_tract_marginal`,
`subtotal_moe_flagged`, `rake_to_marginals`,
`compute_puma_joint`, `joint_to_column_major`,
`column_major_to_joint`, `bin_acs_row`, `proportional_from_brackets`
all unchanged. The `tests/test_core_btr_parity.py` byte-identity
suite remains green (10/10 plus 1 live-PUMS auto-skip).

Marion benchmark (`tests/test_marion_benchmark.py`) also remains
green (8/8).

Core_BTR is unmodified. `git status` in `C:\Users\ndste\Core_BTR`
shows zero source changes introduced by this session.

## Census PUMS API outage (continuing from session 3)

The Census PUMS endpoint has been returning HTTP 500 on every probe
since session 3 fixture generation. Cross-checked across multiple
states (17, 18, 48); ACS5 endpoint is healthy. Mid-session retries
during session 4 also failed.

Impact:
- The live-PUMS parity test from session 3
  (`test_puma_crosstab_byte_identical_live_pums`) remains skipped.
- The session 4 live-PUMS regression tests
  (`test_decompose_lake_county_renter_realistic_shape`,
  `test_e2e_live_pums`) are similarly skipped.
- The synthetic e2e test passes — proves compositional correctness
  even without live data.

Re-running `python scripts/generate_lake_county_fixtures.py` once
the endpoint recovers will regenerate `tests/fixtures/lake_county_pums.parquet`
and auto-activate all four PUMS-conditional tests. None of them
require code changes; the fixture's existence is the only gate.

## Things deferred to future sessions

1. **Multi-vintage / historical trend support.** Would require the
   ACS fetcher's cache key to include vintage (already does) but
   crosstab APIs to take a vintage argument and either rake against a
   historical PUMA prior or reproject across PUMA20 / PUMA10 boundary
   changes. Not started.
2. **Market-segment overlay** (Affordable / Workforce / Market Rate /
   Luxury) variants on the lifestage grid. The cell-grid shape
   supports it — every lifestage cell already keys on
   `(age_bin, income_bin)`, so income-segmented variants are a grid
   replacement, not a code change. Future session ships an
   alternate `RCLCO_INCOME_SEGMENTED_LIFESTAGE_GRID` constant.
3. **3-D crosstabs** (income × age × hh_size). The IPF rake is 2-D;
   N-D rake is a different algorithm. Out of scope.
4. **Frontend.** Phase 2.
5. **Public BTR bin-scheme submodule.** Per session 4 read-back
   decision: don't ship until a second consumer actually needs the
   same configs. `tests/_btr_configs.py` is the placeholder.
6. **Carry-over hazards from REFACTOR_NOTES.md** (the
   `_last_county_fips` global, `sys.exit()` in library code, ACS
   cache key not including variable list). Unchanged this session.
7. **`LifestageGrid.from_records()` classmethod** for JSON / YAML
   config-file loading. Add when a consumer actually needs it.

## Stopping conditions encountered

- **Census PUMS endpoint outage** — same as session 3. Did not block
  any session-4 work; synthetic-only testing on the affected
  primitives.
- All other stopping conditions cleared:
  - byte-identity gates from session 3 still green
  - Marion benchmark still green
  - owner ACS variable codes match the expected `_002E`/`_003E…`
    pattern (verified via direct API metadata probes in the
    read-back)
  - `B25003_002E` was already in defaults
  - PUMS `HINCP`/`TEN`/`WGTP` were already in defaults
  - end-to-end integration test passes every assertion
  - wheel build untouched (no new bulk data shipped this session)
