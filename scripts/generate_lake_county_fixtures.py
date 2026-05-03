"""
Generate Lake County, IL fixtures for the Core_BTR parity tests.

Run once to capture:
  - tests/fixtures/lake_county_tracts.parquet — Lake County tract rows
    from a 2024 ACS5 fetch with the BTR variable list (ACS_BG_DEFAULT_VARS
    plus B25009 renter HH-size codes). Only Lake County rows kept.
  - tests/fixtures/lake_county_pums.parquet — full Lake County PUMS
    records joined H+P (output of fetch_pums_data) for the PUMAs that
    overlap Lake County.

Idempotent: if fixtures exist, exits without re-fetching.

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

# Match Core_BTR's pipeline.py:74-75 variable list so the parity tests
# operate on the same column set Core_BTR's pipeline sees in production.
B25009_RENTER_VARS = [f"B25009_{n:03d}E" for n in (10, 11, 12, 13, 14, 15, 16, 17)]
ACS_BTR_VARS = list(ACS_BG_DEFAULT_VARS) + B25009_RENTER_VARS

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
            variables=ACS_BTR_VARS,
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
