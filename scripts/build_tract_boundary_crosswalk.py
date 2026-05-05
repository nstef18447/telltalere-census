"""
Build the 2010 <-> 2020 census tract relationship parquet from Census's
national pipe-delimited text file.

Output: build/tract_boundary_crosswalk_2010_to_2020.parquet

The Census-published file
(https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/tab20_tract20_tract10_natl.txt)
is pipe-delimited, BOM-prefixed UTF-8, ~18 MB raw. Each row represents the
intersection of a 2020 tract with a 2010 tract. AREALAND_PART is the land
area of that intersection in square meters; weights are derived as
intersection-area / source-tract-land-area.

Important: weights are **area-based, not population-based**. The
relationship file does not include population data. Population-weighted
allocation would require joining 2020 census block populations to the
intersection geometries — substantially more complex. The area-weight
approximation is standard in tract-harmonization research and accurate
when populations are roughly uniform within the boundary changes
(typically true for tract subdivisions, less true for tracts that
crossed a previously-vacant new development). This caveat is documented
in `boundaries.harmonize_to_2020_boundaries` and the module docstring.

The output parquet has columns:
  geoid_2020          (str, 11 chars)
  geoid_2010          (str, 11 chars)
  state_fips          (str, 2 chars)
  county_fips         (str, 3 chars)
  arealand_part_sqm   (Int64, intersection land area in square meters)
  arealand_2020_sqm   (Int64, total 2020 tract land area)
  arealand_2010_sqm   (Int64, total 2010 tract land area)
  area_weight_2010    (Float64, AREALAND_PART / AREALAND_TRACT_10)
                      sums to ~1.0 across rows for the same 2010 tract;
                      use this when allocating a 2010 tract value across
                      its 2020 successors.
  area_weight_2020    (Float64, AREALAND_PART / AREALAND_TRACT_20)
                      sums to ~1.0 across rows for the same 2020 tract;
                      use this when checking how a 2020 tract's land
                      composition maps back to 2010.
  relationship_type   (str, one of {'unchanged', 'split', 'merge', 'partial'})

relationship_type rules:
  - 'unchanged': the 2010 tract maps to exactly one 2020 tract AND that
    2020 tract maps from exactly that 2010 tract.
  - 'split':     the 2010 tract maps to multiple 2020 tracts, but each of
    those 2020 tracts has only this single 2010 contributor.
  - 'merge':     the 2020 tract has multiple 2010 contributors, but this
    row's 2010 tract maps only to this 2020 tract.
  - 'partial':   neither side is one-to-one (many-to-many overlap).

Pandas metadata:
  build_date: ISO 8601 timestamp of script execution.
  source_url: the Census URL.
  total_rows: row count.
  weight_basis: 'area' (documents that weights are area-based, not population).
"""

from __future__ import annotations

import datetime
import io
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests


SOURCE_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/tract/"
    "tab20_tract20_tract10_natl.txt"
)
OUT_DIR = Path(__file__).resolve().parent.parent / "build"
OUT_PATH = OUT_DIR / "tract_boundary_crosswalk_2010_to_2020.parquet"


def _download(url: str) -> str:
    """Fetch the file as a UTF-8 string. ~18 MB; ~5-15s on a typical
    connection."""
    print(f"Downloading {url} ...")
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    # Census uses BOM-prefixed UTF-8 — let pandas / utf-8-sig handle it
    return resp.content.decode("utf-8-sig")


def _parse(text: str) -> pd.DataFrame:
    print("Parsing pipe-delimited rows ...")
    df = pd.read_csv(io.StringIO(text), sep="|")
    print(f"  parsed {len(df):,} rows, {len(df.columns)} cols")
    print(f"  columns: {list(df.columns)}")

    # Sanity-check column presence
    required = {
        "GEOID_TRACT_20", "GEOID_TRACT_10",
        "AREALAND_TRACT_20", "AREALAND_TRACT_10", "AREALAND_PART",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Census file missing expected columns: {missing}")
    return df


def _compute(raw: pd.DataFrame) -> pd.DataFrame:
    print("Computing weights and relationship types ...")

    # Zero-pad GEOIDs to canonical width (11 chars). They arrive as integers
    # from Census because they're all-digit; pandas reads them as int64.
    g20 = raw["GEOID_TRACT_20"].astype(str).str.zfill(11)
    g10 = raw["GEOID_TRACT_10"].astype(str).str.zfill(11)

    arealand_part = raw["AREALAND_PART"].astype("Int64")
    arealand_20 = raw["AREALAND_TRACT_20"].astype("Int64")
    arealand_10 = raw["AREALAND_TRACT_10"].astype("Int64")

    # Weights — guard zero denominators (handful of tracts are pure water)
    weight_2010 = (arealand_part.astype("Float64") / arealand_10.astype("Float64"))
    weight_2010 = weight_2010.where(arealand_10 != 0, other=pd.NA)
    weight_2020 = (arealand_part.astype("Float64") / arealand_20.astype("Float64"))
    weight_2020 = weight_2020.where(arealand_20 != 0, other=pd.NA)

    out = pd.DataFrame({
        "geoid_2020": g20,
        "geoid_2010": g10,
        "state_fips": g20.str[:2],
        "county_fips": g20.str[2:5],
        "arealand_part_sqm": arealand_part,
        "arealand_2020_sqm": arealand_20,
        "arealand_2010_sqm": arealand_10,
        "area_weight_2010": weight_2010,
        "area_weight_2020": weight_2020,
    })

    # relationship_type: count rows per side
    rows_per_2010 = out.groupby("geoid_2010", observed=True).size()
    rows_per_2020 = out.groupby("geoid_2020", observed=True).size()
    out["_n_2020_for_this_2010"] = out["geoid_2010"].map(rows_per_2010)
    out["_n_2010_for_this_2020"] = out["geoid_2020"].map(rows_per_2020)

    one_to_one = (out["_n_2020_for_this_2010"] == 1) & (out["_n_2010_for_this_2020"] == 1)
    one_2010_many_2020 = (out["_n_2020_for_this_2010"] > 1) & (out["_n_2010_for_this_2020"] == 1)
    many_2010_one_2020 = (out["_n_2020_for_this_2010"] == 1) & (out["_n_2010_for_this_2020"] > 1)

    out["relationship_type"] = "partial"
    out.loc[one_to_one, "relationship_type"] = "unchanged"
    out.loc[one_2010_many_2020, "relationship_type"] = "split"
    out.loc[many_2010_one_2020, "relationship_type"] = "merge"

    out = out.drop(columns=["_n_2020_for_this_2010", "_n_2010_for_this_2020"])

    # Diagnostics
    rel_counts = out["relationship_type"].value_counts().to_dict()
    print(f"  relationship_type breakdown: {rel_counts}")
    print(f"  unique 2020 tracts: {out['geoid_2020'].nunique():,}")
    print(f"  unique 2010 tracts: {out['geoid_2010'].nunique():,}")

    # Cross-check: weight_2010 sums per 2010 tract should be ~1.0
    sums = out.groupby("geoid_2010", observed=True)["area_weight_2010"].sum()
    drift = (sums - 1.0).abs()
    print(
        f"  area_weight_2010 sum-to-one check: max drift = {drift.max():.6f} "
        f"(median {drift.median():.2e})"
    )

    return out


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)

    metadata = {
        b"build_date": datetime.datetime.now(datetime.timezone.utc).isoformat().encode(),
        b"source_url": SOURCE_URL.encode(),
        b"total_rows": str(len(df)).encode(),
        b"weight_basis": b"area",
    }
    existing = table.schema.metadata or {}
    new_schema = table.schema.with_metadata({**existing, **metadata})
    table = table.replace_schema_metadata(new_schema.metadata)

    pq.write_table(table, path, compression="snappy")
    print(f"Wrote {path} ({path.stat().st_size / (1024 * 1024):.2f} MB)")


def main() -> None:
    text = _download(SOURCE_URL)
    raw = _parse(text)
    out = _compute(raw)
    _write(out, OUT_PATH)


if __name__ == "__main__":
    main()
