"""
Build per-state tract polygon parquet files for upload to the package's
GitHub release.

For each US state + DC + Puerto Rico, fetches generalized 1:500k tract
boundaries from the TIGERweb ArcGIS REST endpoint (paginated), simplifies
each polygon with `shapely.simplify(tolerance=0.0005)` (~55 m at mid-
latitudes — invisible at typical map zoom levels), and writes a
per-state parquet to `build/tracts_{state_fips}.parquet`.

These per-state files are intended as GitHub release assets. The
runtime helper `telltalere_census.geometry.download_tract_polygons`
fetches them on demand into a user-cache directory.

Why per-state shards (not a single national parquet):
  - Lazy load: a consumer that needs only IL data downloads ~2 MB,
    not the full ~50 MB.
  - Resumability: an interrupted build resumes from the next state.
  - Graceful asset upload: 52 individual release assets are easier to
    re-upload than a single multi-tens-of-MB blob.

Output schema per state file:
    state_fips   (str, zero-padded to 2 chars)
    county_fips  (str, zero-padded to 3 chars)
    tract_fips   (str, zero-padded to 6 chars — local tract code)
    geoid        (str, 11 chars — state+county+tract)
    centroid_lon (float, WGS84)
    centroid_lat (float, WGS84)
    polygon_wkb  (bytes — Well-Known Binary, EPSG:4326)

Pandas metadata (parquet schema):
    build_date     ISO 8601 UTC timestamp of the fetch
    source         TIGERweb endpoint URL
    simplify_tol   shapely.simplify tolerance applied (degrees)
    package_ver    telltalere-census version that built this artifact

Resumable: skips any state whose `build/tracts_{state_fips}.parquet`
already exists. Pass --force to re-fetch + overwrite all states (or
--state X to re-fetch one state).

Usage:
    python scripts/build_tract_polygons.py            # all 52, resumable
    python scripts/build_tract_polygons.py --state 17 # just IL
    python scripts/build_tract_polygons.py --force    # re-fetch everything
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from telltalere_census import __version__
from telltalere_census.geometry import _all_state_fips


TIGER_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "Generalized_ACS2024/Tracts_Blocks/MapServer/4/query"
)
TIGER_BATCH = 2000
SIMPLIFY_TOLERANCE = 0.0005   # degrees, ~55 m at mid-latitudes
HTTP_TIMEOUT = 180

BUILD_DIR = REPO_ROOT / "build"

logger = logging.getLogger("build_tract_polygons")


def fetch_state_tracts(state_fips: str) -> list[dict]:
    """Page TIGERweb for one state's tract features. Returns the GeoJSON
    feature list. Adapted from Core_BTR's hotspot_index.fetch_state_tracts."""
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": f"STATE='{state_fips}'",
            "outFields": "GEOID,STATE,COUNTY,TRACT",
            "returnGeometry": "true",
            "f": "geojson",
            "resultRecordCount": str(TIGER_BATCH),
            "resultOffset": str(offset),
        }
        logger.info("  TIGERweb state=%s offset=%d", state_fips, offset)
        resp = requests.get(TIGER_BASE, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(
                f"TIGERweb error state={state_fips}: {payload['error']}"
            )
        batch = payload.get("features", [])
        out.extend(batch)
        if len(batch) < TIGER_BATCH:
            break
        offset += TIGER_BATCH
    return out


def features_to_dataframe(features: list[dict]) -> pd.DataFrame:
    from shapely.geometry import shape
    rows: list[dict] = []
    for f in features:
        props = f["properties"]
        geom = shape(f["geometry"])
        if not geom.is_valid:
            geom = geom.buffer(0)  # cheap topology fix
        simplified = geom.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
        # Some pathological polygons collapse to empty under simplify; fall
        # back to the original geometry rather than ship a null cell.
        if simplified.is_empty or not simplified.is_valid:
            simplified = geom
        centroid = simplified.centroid
        state = str(props["STATE"]).zfill(2)
        county = str(props["COUNTY"]).zfill(3)
        tract = str(props["TRACT"]).zfill(6)
        rows.append({
            "state_fips":  state,
            "county_fips": county,
            "tract_fips":  tract,
            "geoid":       state + county + tract,
            "centroid_lon": float(centroid.x),
            "centroid_lat": float(centroid.y),
            "polygon_wkb":  bytes(simplified.wkb),
        })
    return pd.DataFrame(rows)


def write_state_parquet(df: pd.DataFrame, state_fips: str) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BUILD_DIR / f"tracts_{state_fips}.parquet"

    table = pa.Table.from_pandas(df, preserve_index=False)
    metadata = {
        b"build_date":   datetime.now(timezone.utc).isoformat().encode("utf-8"),
        b"source":       TIGER_BASE.encode("utf-8"),
        b"simplify_tol": str(SIMPLIFY_TOLERANCE).encode("utf-8"),
        b"package_ver":  __version__.encode("utf-8"),
    }
    # Merge our metadata with whatever pyarrow already attached
    existing = table.schema.metadata or {}
    merged = {**existing, **metadata}
    table = table.replace_schema_metadata(merged)
    pq.write_table(table, out_path, compression="snappy")
    return out_path


def build_state(state_fips: str, *, force: bool) -> dict:
    out_path = BUILD_DIR / f"tracts_{state_fips}.parquet"
    if out_path.exists() and not force:
        size_kb = out_path.stat().st_size / 1024
        logger.info("State %s already built (%.1f KB) — skipping", state_fips, size_kb)
        return {"state_fips": state_fips, "rows": None, "size_kb": size_kb, "skipped": True}

    t0 = time.time()
    features = fetch_state_tracts(state_fips)
    df = features_to_dataframe(features)
    out_path = write_state_parquet(df, state_fips)
    elapsed = time.time() - t0
    size_kb = out_path.stat().st_size / 1024
    logger.info("State %s: %d tracts, %.1f KB, %.1fs",
                state_fips, len(df), size_kb, elapsed)
    return {
        "state_fips": state_fips, "rows": len(df), "size_kb": size_kb,
        "skipped": False, "elapsed_s": elapsed,
    }


def build(states: Iterable[str], *, force: bool) -> list[dict]:
    states = list(states)
    summaries: list[dict] = []
    t_start = time.time()
    for i, st in enumerate(states, 1):
        logger.info("[%d/%d] state=%s", i, len(states), st)
        try:
            summary = build_state(st, force=force)
        except Exception as exc:
            logger.error("State %s FAILED: %r", st, exc)
            summaries.append({"state_fips": st, "error": repr(exc)})
            continue
        summaries.append(summary)
    elapsed = time.time() - t_start

    total_kb = sum(s.get("size_kb", 0) for s in summaries if "error" not in s)
    n_failed = sum(1 for s in summaries if "error" in s)
    n_skipped = sum(1 for s in summaries if s.get("skipped"))
    n_built = len(summaries) - n_failed - n_skipped
    logger.info(
        "DONE — %d states (%d built, %d skipped, %d failed) in %.1fs. "
        "Total build/ size: %.1f MB",
        len(summaries), n_built, n_skipped, n_failed, elapsed, total_kb / 1024,
    )

    # Write a manifest JSON next to the parquets so `scripts/upload_release.sh`
    # (or the operator) can verify which state shards are ready.
    manifest_path = BUILD_DIR / "manifest.json"
    manifest_path.write_text(json.dumps({
        "package_ver": __version__,
        "build_date_utc": datetime.now(timezone.utc).isoformat(),
        "simplify_tolerance_deg": SIMPLIFY_TOLERANCE,
        "tiger_endpoint": TIGER_BASE,
        "state_summaries": summaries,
    }, indent=2))
    logger.info("Wrote manifest: %s", manifest_path.relative_to(REPO_ROOT))
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", action="append",
        help="2-digit state FIPS to build. Repeatable. Default: all 52.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch and overwrite even if the per-state parquet exists.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    states = args.state or _all_state_fips()
    states = [s.zfill(2) for s in states]
    build(states, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
