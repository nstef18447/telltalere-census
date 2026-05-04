"""
Generate Lake County, IL fixtures for the parity / e2e tests.

Run once to capture:
  - tests/fixtures/lake_county_tracts.parquet — Lake County tract rows
    from a 2024 ACS5 fetch with `ACS_BG_DEFAULT_VARS`. Lake County rows
    only. As of session 4 the default variable list covers both renter
    and owner B25118/B25007/B25009 brackets, so a single tract fixture
    serves both tenures — consumers select a tenure via authoritative
    total (B25003_002E for owner, B25003_003E for renter) and the
    appropriate BracketConfig.
  - tests/fixtures/lake_county_pums.parquet — full Lake County PUMS
    records joined H+P (output of fetch_pums_data) for the PUMAs that
    overlap Lake County. Both tenures present in one file; consumers
    filter via `df["TEN"].isin([1, 2])` for owners or `[3, 4]` for
    renters. Splitting into separate owner/renter files would duplicate
    data and add maintenance burden — the package's design pushes
    tenure filtering to the call site already.

Idempotent: if fixtures exist, exits without re-fetching. Delete the
fixture files (and the corresponding `data/acs5_*` runtime cache) to
force a re-fetch — useful when `ACS_BG_DEFAULT_VARS` has changed and
the cached snapshot is missing new columns.

Run from repo root:
    python scripts/generate_lake_county_fixtures.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from telltalere_census.acs_fetch import fetch_acs_data
from telltalere_census.county import resolve_county
from telltalere_census.puma_crosswalk import get_pumas_for_county
from telltalere_census.pums_fetch import fetch_pums_data
from telltalere_census.variables import ACS_BG_DEFAULT_VARS

LAKE_COUNTY_FIPS = "17097"
STATE_FIPS = "17"
COUNTY_FIPS = "097"
ACS_VINTAGE = 2024
ACS_TYPE = "acs5"

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
TRACT_FIXTURE = FIXTURE_DIR / "lake_county_tracts.parquet"
PUMS_FIXTURE = FIXTURE_DIR / "lake_county_pums.parquet"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    if TRACT_FIXTURE.exists() and PUMS_FIXTURE.exists():
        print(f"Fixtures already present at {FIXTURE_DIR}; nothing to do.")
        return 0

    if not TRACT_FIXTURE.exists():
        print(f"Fetching tract-level ACS5 {ACS_VINTAGE} for state {STATE_FIPS}...")
        df = fetch_acs_data(
            state_fips=STATE_FIPS,
            variables=ACS_BG_DEFAULT_VARS,
            geography="tract",
            vintage=ACS_VINTAGE,
            acs_type=ACS_TYPE,
            include_moe=True,
        )
        # Filter to Lake County rows. Tract GEOIDs are 11 chars =
        # state(2)+county(3)+tract(6). Lake County prefix = "17097".
        lake = df[df["GEOID"].str.startswith(LAKE_COUNTY_FIPS)].copy()
        if lake.empty:
            print(f"ERROR: no Lake County rows found in {len(df)} state rows", file=sys.stderr)
            return 1
        lake.to_parquet(TRACT_FIXTURE, index=False)
        print(f"Saved {len(lake)} Lake County tract rows to {TRACT_FIXTURE}")
    else:
        print(f"Tract fixture already at {TRACT_FIXTURE}")

    if not PUMS_FIXTURE.exists():
        print(f"Fetching PUMS for state {STATE_FIPS}, Lake County PUMAs...")
        state, county, _, _ = resolve_county(fips=LAKE_COUNTY_FIPS, acs_year=ACS_VINTAGE)
        pumas = get_pumas_for_county(state, county)
        print(f"Lake County PUMAs: {pumas}")
        try:
            pums = fetch_pums_data(
                state_fips=state,
                puma_codes=pumas,
                acs_type=ACS_TYPE,
                acs_year=ACS_VINTAGE,
            )
        except Exception as exc:
            print(
                f"WARNING: PUMS fetch failed ({exc!r}). The Census PUMS endpoint "
                "is occasionally 500-throttled. Re-run this script later to "
                "complete the live-data fixture; the tract-level parity tests "
                "still pass without it.",
                file=sys.stderr,
            )
            return 0
        pums.to_parquet(PUMS_FIXTURE, index=False)
        print(f"Saved {len(pums)} PUMS rows to {PUMS_FIXTURE}")
    else:
        print(f"PUMS fixture already at {PUMS_FIXTURE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
