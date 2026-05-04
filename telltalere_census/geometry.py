"""
Tract-polygon and tract-to-PUMA crosswalk loaders.

Two distinct data sources with different distribution strategies:

  - **tract-to-PUMA crosswalk** ships inside the wheel
    (`telltalere_census/data/tract_to_puma_2020.parquet`, ~860 KB, all 50
    states + DC + PR). Loaded zero-cost on first call.

  - **tract polygons** do NOT ship inside the wheel. Per-state parquet
    files are produced by the build script
    (`scripts/build_tract_polygons.py`) and uploaded to the package's
    GitHub release. The runtime helper `download_tract_polygons` fetches
    them on demand into a user-cache directory; `load_tract_polygons`
    reads from that cache. This keeps the wheel small (~1 MB) while
    making 50-state coverage available with one download call.

Why two strategies: the crosswalk is small, immutable per vintage, and
needed by every consumer that joins tracts to PUMAs (cheap to ship).
The polygons are large (~80 MB across all states even after generalized
1:500k boundaries + shapely.simplify), and not every consumer needs them
— a renter-demographics inspector may only render one MSA's worth.

Vintages and stability:
  - Tract polygons: TIGERweb Generalized_ACS2024 (1:500k generalized
    boundaries). Shipped with `shapely.simplify(tolerance=0.0005)` applied
    in the build, which trims ~30-50% of polygon detail without visible
    loss at typical map zoom levels. Build date is stamped into each
    state parquet's pandas metadata under the `build_date` key.
  - Tract-to-PUMA crosswalk: 2020 Census, matches the PUMS PUMA20
    boundaries used elsewhere in the package.

Geopandas is NOT a hard dependency. The canonical shape is a plain
DataFrame with a `polygon_wkb` column (bytes). For GeoDataFrame-style
loading, install the `[geo]` extra and use `load_tract_polygons_as_gdf`
which imports geopandas inside the function and surfaces a clear
ImportError if the extra is missing.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import pandas as pd
import requests

from telltalere_census import __version__
from telltalere_census.cache import package_data_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Release URL configuration
# ---------------------------------------------------------------------------

# Placeholder defaults — override via env vars once the GitHub release exists.
# The build script writes per-state parquet files named tracts_{state_fips}.parquet
# to a build/ directory; those are uploaded as release assets.
_DEFAULT_RELEASE_OWNER = "REPLACE_ME"
_DEFAULT_RELEASE_TAG = f"v{__version__}"
_ENV_RELEASE_OWNER = "TELLTALERE_CENSUS_RELEASE_OWNER"
_ENV_RELEASE_TAG = "TELLTALERE_CENSUS_RELEASE_TAG"
_ENV_GEOMETRY_CACHE = "TELLTALERE_CENSUS_GEOMETRY_CACHE"


def _release_url(state_fips: str) -> str:
    """
    Build the per-state polygon release-asset URL.

    Override the defaults via environment variables:
      - TELLTALERE_CENSUS_RELEASE_OWNER  (default: 'REPLACE_ME')
      - TELLTALERE_CENSUS_RELEASE_TAG    (default: 'v{__version__}')
    """
    owner = os.environ.get(_ENV_RELEASE_OWNER, _DEFAULT_RELEASE_OWNER)
    tag = os.environ.get(_ENV_RELEASE_TAG, _DEFAULT_RELEASE_TAG)
    return (
        f"https://github.com/{owner}/telltalere-census/"
        f"releases/download/{tag}/tracts_{state_fips}.parquet"
    )


# ---------------------------------------------------------------------------
# Geometry cache directory
# ---------------------------------------------------------------------------

def _geometry_cache_dir(explicit: Optional[Path] = None) -> Path:
    """
    Resolve where downloaded per-state tract parquets live.

    Resolution order:
      1. Explicit `cache_dir` argument
      2. TELLTALERE_CENSUS_GEOMETRY_CACHE env var
      3. platformdirs.user_cache_dir('telltalere_census') / 'geometry'
    """
    if explicit is not None:
        d = Path(explicit)
    elif os.environ.get(_ENV_GEOMETRY_CACHE):
        d = Path(os.environ[_ENV_GEOMETRY_CACHE])
    else:
        try:
            from platformdirs import user_cache_dir  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "platformdirs is required for the default geometry cache "
                "directory. Install it via `pip install platformdirs`, set "
                f"the {_ENV_GEOMETRY_CACHE} env var, or pass cache_dir="
                "explicitly to load/download_tract_polygons."
            ) from exc
        d = Path(user_cache_dir("telltalere_census")) / "geometry"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_parquet_path(cache_dir: Path, state_fips: str) -> Path:
    return cache_dir / f"tracts_{state_fips}.parquet"


def _normalize_state_arg(
    state_fips: Union[str, Iterable[str], None],
) -> Optional[list[str]]:
    """Normalize the user-facing state_fips argument to a list of zero-padded
    2-char strings, or None for 'all states'."""
    if state_fips is None:
        return None
    if isinstance(state_fips, str):
        if state_fips.lower() == "all":
            return None
        return [state_fips.zfill(2)]
    return [str(s).zfill(2) for s in state_fips]


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------

def download_tract_polygons(
    state_fips: Union[str, Iterable[str]] = "all",
    cache_dir: Optional[Path] = None,
    *,
    force: bool = False,
    timeout: int = 180,
) -> list[Path]:
    """
    Download per-state tract polygon parquet(s) from the package GitHub
    release into the geometry cache directory.

    Args:
        state_fips: 2-digit state FIPS code, list of codes, or the literal
            string `"all"` (or a Python list containing all 52 codes) to
            fetch every state + DC + PR. Default `"all"`.
        cache_dir: override for the geometry cache directory. See
            `_geometry_cache_dir` for resolution rules.
        force: re-download even if the per-state parquet is already
            present.
        timeout: per-request HTTP timeout (seconds).

    Returns:
        List of paths to the per-state parquet files that are now
        present in the cache (whether freshly downloaded or already
        cached when force=False).

    Raises:
        requests.HTTPError on a failed download. The release URL is built
        from environment variables; see module docstring for the relevant
        env vars and their defaults. A 404 typically means the release
        owner/tag env vars are pointing at a release that doesn't exist
        yet, or the per-state asset wasn't uploaded.
    """
    requested = _normalize_state_arg(state_fips)
    if requested is None:
        requested = _all_state_fips()

    cache = _geometry_cache_dir(cache_dir)
    out_paths: list[Path] = []
    for st in requested:
        target = _state_parquet_path(cache, st)
        if target.exists() and not force:
            logger.info("Already cached: %s", target.name)
            out_paths.append(target)
            continue
        url = _release_url(st)
        logger.info("Downloading %s from %s", target.name, url)
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        out_paths.append(target)
    return out_paths


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_tract_polygons(
    state_fips: Union[str, Iterable[str], None] = None,
    cache_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Load tract polygons (one row per tract) from the local geometry cache.

    The polygons themselves do NOT ship inside the wheel — they are
    fetched on demand by `download_tract_polygons`. Polygons are encoded
    as Well-Known-Binary in the `polygon_wkb` column. To convert one to
    GeoJSON, call `wkb_to_geojson(row.polygon_wkb)`. To load as a
    GeoDataFrame, use `load_tract_polygons_as_gdf` (requires the `[geo]`
    extra).

    TIGERweb vintage stamp: each per-state parquet records its build
    date in pandas metadata under the `build_date` key. The polygons
    derive from the TIGERweb `Generalized_ACS2024` 1:500k generalized
    boundaries with `shapely.simplify(tolerance=0.0005)` applied at
    build time.

    Args:
        state_fips: 2-digit state FIPS code or list of codes. None (or
            "all") loads every cached state — note that "all" raises if
            any state is missing from the cache; use it only after a
            full `download_tract_polygons("all")`.
        cache_dir: override for the geometry cache directory.

    Returns:
        DataFrame with columns:
            state_fips, county_fips, tract_fips, geoid,
            centroid_lon, centroid_lat, polygon_wkb (bytes)

    Raises:
        FileNotFoundError: when one or more requested states have no
            parquet in the cache. The error message includes the exact
            `download_tract_polygons` call that would fix it.
    """
    requested = _normalize_state_arg(state_fips)
    cache = _geometry_cache_dir(cache_dir)
    if requested is None:
        # Load everything currently cached
        candidates = sorted(cache.glob("tracts_*.parquet"))
        if not candidates:
            raise FileNotFoundError(
                f"No tract polygon parquets in {cache}. Call "
                "`download_tract_polygons('all')` first to populate the cache."
            )
        return _read_concat(candidates)

    missing = [st for st in requested if not _state_parquet_path(cache, st).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing tract polygon parquet(s) for state(s) {missing} in "
            f"{cache}. Call `download_tract_polygons({missing!r})` to fetch."
        )
    return _read_concat([_state_parquet_path(cache, st) for st in requested])


def _read_concat(paths: list[Path]) -> pd.DataFrame:
    if len(paths) == 1:
        df = pd.read_parquet(paths[0])
    else:
        df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)
    return df


def load_tract_to_puma_crosswalk(
    state_fips: Union[str, Iterable[str], None] = None,
) -> pd.DataFrame:
    """
    Load the 2020 tract-to-PUMA crosswalk shipped with the package.

    Args:
        state_fips: optional state filter. None loads all states.

    Returns:
        DataFrame with columns: state_fips, county_fips, tract_fips,
        tract_geoid, puma_code. All string-typed and zero-padded
        (state=2, county=3, tract=6, puma=5; tract_geoid=11).
    """
    path = package_data_path("tract_to_puma_2020.parquet")
    if not path.exists():
        raise FileNotFoundError(
            f"tract_to_puma_2020.parquet missing from package data at {path}. "
            "Reinstall the package, or run `python "
            "scripts/build_tract_puma_crosswalk.py` from the repo to rebuild."
        )
    df = pd.read_parquet(path)
    requested = _normalize_state_arg(state_fips)
    if requested is not None:
        df = df[df["state_fips"].isin(requested)].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def wkb_to_geojson(wkb: bytes) -> dict[str, Any]:
    """
    Convert a Well-Known-Binary polygon to a GeoJSON-style dict (Polygon
    or MultiPolygon).

    Coordinates emit as `[lon, lat]` per the GeoJSON spec. Frontend code
    that wants `[lat, lon]` (e.g. Leaflet Polygon components) must swap
    on its side.

    Requires `shapely`, which the package transitively depends on via
    pyarrow's geo plugins on some installs but not others — this function
    imports it lazily so the rest of the package stays usable when
    shapely is absent.
    """
    try:
        import shapely
        from shapely.geometry import mapping as shapely_mapping
    except ImportError as exc:
        raise ImportError(
            "shapely is required for wkb_to_geojson. "
            "Install via `pip install shapely` or `pip install telltalere-census[geo]`."
        ) from exc
    return shapely_mapping(shapely.from_wkb(wkb))


def load_tract_polygons_as_gdf(
    state_fips: Union[str, Iterable[str], None] = None,
    cache_dir: Optional[Path] = None,
):
    """
    Load tract polygons as a GeoDataFrame (geopandas) with a proper
    geometry column (EPSG:4326).

    Requires the `[geo]` extra: `pip install telltalere-census[geo]`.
    The polygons themselves still come from the local cache (run
    `download_tract_polygons` first).

    Args:
        state_fips: 2-digit FIPS, list of FIPS, or None for all cached.
        cache_dir: override for the geometry cache directory.

    Returns:
        geopandas.GeoDataFrame with columns:
            state_fips, county_fips, tract_fips, geoid,
            centroid_lon, centroid_lat, geometry (shapely Polygon/MultiPolygon)
        CRS = EPSG:4326 (WGS84).
    """
    try:
        import geopandas as gpd  # type: ignore
        import shapely
    except ImportError as exc:
        raise ImportError(
            "geopandas and shapely are required for load_tract_polygons_as_gdf. "
            "Install the optional extras: `pip install telltalere-census[geo]`."
        ) from exc

    df = load_tract_polygons(state_fips=state_fips, cache_dir=cache_dir)
    geom = df["polygon_wkb"].map(shapely.from_wkb)
    out = df.drop(columns=["polygon_wkb"])
    return gpd.GeoDataFrame(out, geometry=geom, crs="EPSG:4326")


# ---------------------------------------------------------------------------
# State FIPS list (the one the build / download cover)
# ---------------------------------------------------------------------------

def _all_state_fips() -> list[str]:
    """All 50 states + DC (11) + Puerto Rico (72) — the universe of the
    tract-to-PUMA crosswalk and the TIGERweb tract layer."""
    from telltalere_census.variables import STATE_FIPS
    fips = set(STATE_FIPS.values())
    fips.add("11")  # DC
    fips.add("72")  # PR (already in STATE_FIPS but defensive)
    return sorted(fips)
