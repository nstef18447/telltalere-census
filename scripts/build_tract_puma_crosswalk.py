"""
Build the tract-to-PUMA crosswalk parquet shipped with the package.

Downloads the Census-published 2020 tract-to-PUMA mapping and writes it
to `telltalere_census/data/tract_to_puma_2020.parquet`. This file ships
inside the wheel — it's small (~300 KB compressed), pure-text, and used
by every consumer that needs to map a tract GEOID to its parent PUMA.

Source:
    https://www2.census.gov/geo/docs/reference/puma2020/2020_Census_Tract_to_2020_PUMA.txt

The text file uses the column names STATEFP, COUNTYFP, TRACTCE, PUMA5CE.
The output parquet normalizes to lowercase and adds a composite GEOID
(state+county+tract = 11 chars) for one-step lookup.

Idempotent: skips download if the parquet already exists. Pass --force
to rebuild.

Run from repo root:
    python scripts/build_tract_puma_crosswalk.py
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from telltalere_census.variables import CROSSWALK_URL

OUTPUT_PATH = REPO_ROOT / "telltalere_census" / "data" / "tract_to_puma_2020.parquet"


def build(force: bool = False) -> Path:
    if OUTPUT_PATH.exists() and not force:
        size_kb = OUTPUT_PATH.stat().st_size / 1024
        print(f"{OUTPUT_PATH.name} already exists ({size_kb:.1f} KB). "
              "Pass --force to rebuild.")
        return OUTPUT_PATH

    print(f"Downloading crosswalk from {CROSSWALK_URL}")
    resp = requests.get(CROSSWALK_URL, timeout=120)
    resp.raise_for_status()

    df = pd.read_csv(io.BytesIO(resp.content), dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]

    # Source columns: STATEFP, COUNTYFP, TRACTCE, PUMA5CE
    # Normalize to canonical zero-padded widths.
    df["state_fips"] = df["STATEFP"].str.zfill(2)
    df["county_fips"] = df["COUNTYFP"].str.zfill(3)
    df["tract_fips"] = df["TRACTCE"].str.zfill(6)
    df["puma_code"] = df["PUMA5CE"].str.zfill(5)
    df["tract_geoid"] = df["state_fips"] + df["county_fips"] + df["tract_fips"]

    out = df[["state_fips", "county_fips", "tract_fips", "tract_geoid", "puma_code"]].copy()
    out = out.sort_values(["state_fips", "county_fips", "tract_fips"]).reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUTPUT_PATH, index=False, compression="snappy")

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {len(out):,} rows ({out['state_fips'].nunique()} states) "
          f"to {OUTPUT_PATH.relative_to(REPO_ROOT)} ({size_kb:.1f} KB)")
    return OUTPUT_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-download and overwrite the existing parquet.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    build(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
