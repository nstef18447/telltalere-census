"""
Bulk ACS cache warming.

Pre-fetches a comprehensive corpus of ACS detailed tables and data profiles
to local parquet files at `~/.telltalere-census/cache/bulk/`. After warming,
the per-fetch path in `multi_vintage.fetch_acs_data_multi_vintage` reads
transparently from the bulk cache and avoids the Census API entirely on
warm reads.

Conventions
-----------
Bulk cache root resolution (first match wins):
  1. explicit `cache_dir` argument
  2. TELLTALERE_CENSUS_BULK_CACHE_DIR env var
  3. ~/.telltalere-census/cache/bulk/

Filename convention:
  {cache_root}/{geography}/{vintage_tag}/{table}_{state_fips}.parquet

Where `vintage_tag` is the bulk-cache string label, distinct from the
public-API integer end-year:
  - 5-year: "{start}-{end}", e.g. "2020-2024" for end-year 2024
  - 1-year: str(year), e.g. "2024"

`state_fips` is the 2-digit FIPS code for state-bound geographies, or "00"
for nation-wide geographies (MSA, ZCTA). For 1-year `state` geography we
fetch each state separately so the filename is the state's own FIPS.

Each parquet contains one whole Census B- or DP-table for that
(geography, vintage, state) cell, fetched via the API's `group(<table>)`
syntax so estimates, MOEs, and annotation columns all arrive in one call.

Failure mode
------------
log + continue. Per-tuple HTTP failures are caught, logged, and retried
once at the end of the run with longer backoff. Catastrophic halts only
on: invalid API key, no API responses at all from preflight probe,
cache dir not writable, < 20 GB free disk.

Concurrency
-----------
ThreadPoolExecutor with `max_workers=5`. A shared token-bucket limiter
caps the aggregate request rate at `target_rate_per_sec` (default 5/s)
across all threads, well under the Census API's documented ceiling.

Variable scope
--------------
The default 25-table set is locked in `DEFAULT_WARM_TABLES`:
21 detailed (B-) tables covering demographics, households, income,
tenure, housing stock, education, employment, transportation; plus 4
data profile (DP-) composite tables. DP tables hit a different endpoint
(`/profile`) — the fetcher selects the right endpoint by table prefix.

Vintage scope
-------------
Defaults match the public-API contract enforced by
`multi_vintage.get_available_vintages`:
  - 5-year: 2013-2017 through 2020-2024 (8 vintages, end-years 2017..2024)
  - 1-year: 2014..2024 excluding 2020 (10 vintages)
Older vintages can be passed explicitly but consumers should be aware
the public `fetch_acs_data_multi_vintage` will refuse them.

Geography scope
---------------
  - 5-year (6): tract, puma, county, place, msa (nation-wide), zcta (nation-wide)
  - 1-year (5): puma, county, place, state, msa (nation-wide)
Block group is NOT in the bulk warm scope — its variable count makes
the parquet count balloon without commensurate downstream demand.
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import pandas as pd
import requests

from telltalere_census.variables import STATE_FIPS


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEFAULT_WARM_TABLES: list[str] = [
    # Demographics (4)
    "B01001", "B01003", "B02001", "B03002",
    # Households (3)
    "B11001", "B11005", "B11016",
    # Income (4)
    "B19001", "B19013", "B19025", "B19301",
    # Tenure & housing (10)
    "B25001", "B25003", "B25007", "B25024", "B25034",
    "B25064", "B25075", "B25077", "B25118",
    # Education (1)
    "B15003",
    # Employment & transportation (3)
    "B23025", "B08301", "B08303",
    # Data profile composites (4)
    "DP02", "DP03", "DP04", "DP05",
]

DEFAULT_WARM_VINTAGES_5YR: list[str] = [
    f"{end_year - 4}-{end_year}" for end_year in range(2017, 2025)
]
# = ['2013-2017', '2014-2018', '2015-2019', '2016-2020', '2017-2021',
#    '2018-2022', '2019-2023', '2020-2024']

DEFAULT_WARM_VINTAGES_1YR: list[int] = [
    y for y in range(2014, 2025) if y != 2020
]
# = [2014, 2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024]

DEFAULT_WARM_GEOGRAPHIES_5YR: list[str] = [
    "tract", "puma", "county", "place", "msa", "zcta",
]

DEFAULT_WARM_GEOGRAPHIES_1YR: list[str] = [
    "puma", "county", "place", "state", "msa",
]

NATION_WIDE_GEOGRAPHIES: frozenset[str] = frozenset({"msa", "zcta"})
NATION_WIDE_STATE_PLACEHOLDER: str = "00"

ENV_BULK_CACHE_DIR: str = "TELLTALERE_CENSUS_BULK_CACHE_DIR"

# Geography -> Census API `for=` clause and whether `in=state:{st}` applies.
# Tested 2026-05-11 against ACS5 2024.
_GEO_API: dict[str, dict] = {
    "tract":  {"for": "tract:*",                                                          "needs_in_state": True},
    "puma":   {"for": "public use microdata area:*",                                      "needs_in_state": True},
    "county": {"for": "county:*",                                                         "needs_in_state": True},
    "place":  {"for": "place:*",                                                          "needs_in_state": True},
    "state":  {"for": "state:{st}",                                                       "needs_in_state": False},
    "msa":    {"for": "metropolitan statistical area/micropolitan statistical area:*",    "needs_in_state": False},
    "zcta":   {"for": "zip code tabulation area:*",                                       "needs_in_state": False},
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class WarmCacheResult:
    completed_count: int
    skipped_count: int
    failed_count: int
    total_duration_sec: float
    failed_tuples: list[tuple] = field(default_factory=list)
    log_path: Optional[Path] = None

    def __str__(self) -> str:
        return (
            f"WarmCacheResult(completed={self.completed_count}, "
            f"skipped={self.skipped_count}, failed={self.failed_count}, "
            f"duration={self.total_duration_sec:.1f}s)"
        )


# ---------------------------------------------------------------------------
# Cache path resolution
# ---------------------------------------------------------------------------

def resolve_bulk_cache_dir(explicit: Optional[Path] = None) -> Path:
    """Resolve the bulk cache root directory, creating it if needed."""
    if explicit is not None:
        p = Path(explicit)
    elif os.environ.get(ENV_BULK_CACHE_DIR):
        p = Path(os.environ[ENV_BULK_CACHE_DIR])
    else:
        p = Path.home() / ".telltalere-census" / "cache" / "bulk"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bulk_parquet_path(
    cache_dir: Path, geography: str, vintage_tag: str,
    table: str, state_fips: str,
) -> Path:
    """Return the parquet path for one (geography, vintage, table, state) tuple."""
    return cache_dir / geography / vintage_tag / f"{table}_{state_fips}.parquet"


# ---------------------------------------------------------------------------
# Vintage label conversion
# ---------------------------------------------------------------------------

def vintage_tag_5yr(end_year: int) -> str:
    """Convert a 5-year ACS end-year (int) to the bulk-cache vintage label."""
    return f"{end_year - 4}-{end_year}"


def parse_vintage_tag_5yr(tag: str) -> int:
    """Parse a '{start}-{end}' bulk vintage label to its end-year."""
    return int(tag.split("-")[1])


def vintage_tag_1yr(year: int) -> str:
    """Convert a 1-year ACS year (int) to the bulk-cache vintage label."""
    return str(year)


# ---------------------------------------------------------------------------
# Token bucket rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Crude token bucket: enforce a minimum gap between successive acquires
    across all calling threads. Suitable for steady ~5 req/sec ceilings.
    """

    def __init__(self, target_rate_per_sec: float):
        self._min_interval = 1.0 / target_rate_per_sec if target_rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

class _RetryableHTTPError(Exception):
    """Wraps a 429 or 5xx response so retry logic can catch it specifically."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class _PermanentHTTPError(Exception):
    """4xx (non-429) response — request will not succeed on retry."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def _with_retry(
    fn: Callable, *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    """Call `fn()` with exponential backoff on _RetryableHTTPError."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except _RetryableHTTPError as e:
            last_exc = e
            if attempt == max_attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.5 + random.random())  # jitter [0.5x, 1.5x]
            logger.warning(
                "Retry %d/%d after %.1fs: %s",
                attempt, max_attempts, delay, e,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Census API URL + endpoint routing
# ---------------------------------------------------------------------------

def _base_url(vintage_year: int, acs_type: str, table: str) -> str:
    """Pick the right Census API endpoint by table prefix."""
    prefix_to_segment = {"B": "", "C": "", "DP": "/profile", "S": "/subject"}
    seg = next((v for k, v in prefix_to_segment.items() if table.startswith(k)), "")
    return f"https://api.census.gov/data/{vintage_year}/acs/{acs_type}{seg}"


def _build_api_params(
    table: str, geography: str, state_fips: Optional[str],
    api_key: Optional[str],
) -> dict:
    cfg = _GEO_API[geography]
    params = {"get": f"group({table})"}
    if geography == "state":
        # Single state per call; state_fips is required.
        if not state_fips:
            raise ValueError("state_fips is required for state geography")
        params["for"] = cfg["for"].format(st=state_fips)
    else:
        params["for"] = cfg["for"]
        if cfg["needs_in_state"]:
            if not state_fips:
                raise ValueError(f"state_fips is required for geography {geography!r}")
            params["in"] = f"state:{state_fips}"
    if api_key:
        params["key"] = api_key
    return params


def _fetch_one_tuple(
    table: str, geography: str, vintage_year: int, acs_type: str,
    state_fips: str, api_key: Optional[str], timeout: float = 180.0,
) -> pd.DataFrame:
    """Hit the Census API once. Returns a typed DataFrame."""
    url = _base_url(vintage_year, acs_type, table)
    # For nation-wide geographies, state_fips is the "00" placeholder; the
    # API call itself doesn't take a state filter.
    api_state_fips = None if geography in NATION_WIDE_GEOGRAPHIES else state_fips
    params = _build_api_params(table, geography, api_state_fips, api_key)
    resp = requests.get(url, params=params, timeout=timeout)
    if resp.status_code == 429 or 500 <= resp.status_code < 600:
        raise _RetryableHTTPError(
            resp.status_code,
            f"{resp.status_code} from {url} ({geography} {vintage_year} {table} st={state_fips}): {resp.text[:200]}",
        )
    if 400 <= resp.status_code < 500:
        raise _PermanentHTTPError(
            resp.status_code,
            f"{resp.status_code} from {url} ({geography} {vintage_year} {table} st={state_fips}): {resp.text[:200]}",
        )
    resp.raise_for_status()
    data = resp.json()
    if not data or len(data) < 1:
        raise RuntimeError(f"Empty response for {table} {geography} {vintage_year} st={state_fips}")
    return _build_dataframe(data, geography)


def _build_dataframe(raw: list[list], geography: str) -> pd.DataFrame:
    """Parse API JSON and apply minimal typing. Sentinel handling matches acs_fetch."""
    headers = raw[0]
    rows = raw[1:]
    df = pd.DataFrame(rows, columns=headers)

    # The `group(...)` response includes a redundant `GEO_ID` column on some
    # vintages; if present, keep it as string.
    # Coerce variable columns to numeric where appropriate. Census variable
    # codes look like `<TABLE>_<NNNN><suffix>` where suffix in {E, EA, M, MA,
    # PE, PEA, PM, PMA}. Numeric: E, M, PE, PM. String: EA, MA, PEA, PMA.
    sentinel_ints = {-555555555, -222222222, -333333333, -666666666, -888888888, -999999999}
    sentinel_strings = {"", "*", "**", "***", "(X)", "null", "N", "-"}
    for col in df.columns:
        is_numeric = (
            col.endswith("E") and not col.endswith("EA")
            or col.endswith("M") and not col.endswith("MA")
            or col.endswith("PE") or col.endswith("PM")
        )
        # Geography ID columns are NOT estimate codes; leave as string.
        is_geo_col = "_" not in col or col in (
            "state", "county", "tract", "block group", "place",
            "public use microdata area", "metropolitan statistical area/micropolitan statistical area",
            "zip code tabulation area",
        )
        if is_geo_col:
            continue
        if not is_numeric:
            continue
        s = df[col].astype(str).str.strip()
        s = s.where(~s.isin(sentinel_strings), other=None)
        n = pd.to_numeric(s, errors="coerce")
        n = n.where(~n.isin(sentinel_ints), other=float("nan"))
        nonna = n.dropna()
        if len(nonna) > 0 and (nonna % 1 == 0).all():
            df[col] = n.astype("Int64")
        else:
            df[col] = n.astype("Float64")

    # Build canonical GEOID for state-bound geographies (parallel to acs_fetch.py).
    if geography == "tract" and {"state", "county", "tract"}.issubset(df.columns):
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["county"].astype(str).str.zfill(3)
            + df["tract"].astype(str).str.zfill(6)
        )
    elif geography == "county" and {"state", "county"}.issubset(df.columns):
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["county"].astype(str).str.zfill(3)
        )
    elif geography == "puma" and "public use microdata area" in df.columns:
        df = df.rename(columns={"public use microdata area": "puma"})
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["puma"].astype(str).str.zfill(5)
        )
    elif geography == "place" and "place" in df.columns:
        df["GEOID"] = (
            df["state"].astype(str).str.zfill(2)
            + df["place"].astype(str).str.zfill(5)
        )
    elif geography == "state" and "state" in df.columns:
        df["GEOID"] = df["state"].astype(str).str.zfill(2)
    elif geography == "msa":
        # Column name is verbatim from the API.
        msa_col = "metropolitan statistical area/micropolitan statistical area"
        if msa_col in df.columns:
            df = df.rename(columns={msa_col: "cbsa"})
            df["GEOID"] = df["cbsa"].astype(str).str.zfill(5)
    elif geography == "zcta" and "zip code tabulation area" in df.columns:
        df = df.rename(columns={"zip code tabulation area": "zcta"})
        df["GEOID"] = df["zcta"].astype(str).str.zfill(5)

    return df


# ---------------------------------------------------------------------------
# Work plan enumeration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _WarmTuple:
    geography: str
    vintage_tag: str
    table: str
    state_fips: str  # "00" for nation-wide geographies
    acs_type: str    # "acs5" or "acs1"
    vintage_year: int


def _enumerate_tuples(
    vintages_5yr: list[str],
    vintages_1yr: list[int],
    geographies_5yr: list[str],
    geographies_1yr: list[str],
    states: list[str],
    tables: list[str],
) -> list[_WarmTuple]:
    out: list[_WarmTuple] = []
    for vintage_tag in vintages_5yr:
        end_year = parse_vintage_tag_5yr(vintage_tag)
        for geo in geographies_5yr:
            for table in tables:
                if geo in NATION_WIDE_GEOGRAPHIES:
                    out.append(_WarmTuple(
                        geography=geo, vintage_tag=vintage_tag, table=table,
                        state_fips=NATION_WIDE_STATE_PLACEHOLDER,
                        acs_type="acs5", vintage_year=end_year,
                    ))
                else:
                    for st in states:
                        out.append(_WarmTuple(
                            geography=geo, vintage_tag=vintage_tag, table=table,
                            state_fips=st, acs_type="acs5", vintage_year=end_year,
                        ))
    for year in vintages_1yr:
        vintage_tag_str = vintage_tag_1yr(year)
        for geo in geographies_1yr:
            for table in tables:
                if geo in NATION_WIDE_GEOGRAPHIES:
                    out.append(_WarmTuple(
                        geography=geo, vintage_tag=vintage_tag_str, table=table,
                        state_fips=NATION_WIDE_STATE_PLACEHOLDER,
                        acs_type="acs1", vintage_year=year,
                    ))
                else:
                    for st in states:
                        out.append(_WarmTuple(
                            geography=geo, vintage_tag=vintage_tag_str, table=table,
                            state_fips=st, acs_type="acs1", vintage_year=year,
                        ))
    return out


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _process_tuple(
    t: _WarmTuple, cache_dir: Path, rate_limiter: _RateLimiter,
    api_key: Optional[str], retry_overrides: Optional[dict] = None,
) -> tuple[str, _WarmTuple, Optional[str]]:
    """Returns (status, tuple, error_message). status in {completed, skipped, failed}."""
    target = bulk_parquet_path(
        cache_dir, t.geography, t.vintage_tag, t.table, t.state_fips,
    )
    if target.exists():
        return ("skipped", t, None)

    def _do_fetch() -> pd.DataFrame:
        rate_limiter.acquire()
        return _fetch_one_tuple(
            table=t.table, geography=t.geography, vintage_year=t.vintage_year,
            acs_type=t.acs_type, state_fips=t.state_fips, api_key=api_key,
        )

    try:
        kwargs = retry_overrides or {}
        df = _with_retry(_do_fetch, **kwargs)
    except _PermanentHTTPError as e:
        return ("failed", t, f"{e.status_code}: {e}")
    except _RetryableHTTPError as e:
        return ("failed", t, f"{e.status_code} after retries: {e}")
    except Exception as e:  # noqa: BLE001 — log + continue
        return ("failed", t, str(e))

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
    except Exception as e:  # noqa: BLE001
        return ("failed", t, f"parquet write failed: {e}")
    return ("completed", t, None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def warm_cache(
    cache_dir: Optional[Path] = None,
    vintages_5yr: Optional[list[str]] = None,
    vintages_1yr: Optional[list[int]] = None,
    geographies_5yr: Optional[list[str]] = None,
    geographies_1yr: Optional[list[str]] = None,
    states: Optional[list[str]] = None,
    tables: Optional[list[str]] = None,
    max_workers: int = 5,
    target_rate_per_sec: float = 5.0,
    api_key: Optional[str] = None,
    log_path: Optional[Path] = None,
    dry_run: bool = False,
    min_free_gb: float = 20.0,
) -> WarmCacheResult:
    """
    Pre-fetch ACS detailed tables and data profiles into the bulk cache.

    See module docstring for the on-disk layout and scope defaults.

    Args:
        cache_dir: override bulk cache root.
        vintages_5yr: list of 5-year bulk vintage tags (e.g. ["2020-2024"]).
            None ⇒ DEFAULT_WARM_VINTAGES_5YR.
        vintages_1yr: list of 1-year vintage years (int).
            None ⇒ DEFAULT_WARM_VINTAGES_1YR.
        geographies_5yr: subset of DEFAULT_WARM_GEOGRAPHIES_5YR.
        geographies_1yr: subset of DEFAULT_WARM_GEOGRAPHIES_1YR.
        states: list of 2-digit state FIPS codes. None ⇒ all 52.
        tables: list of B- or DP-table IDs. None ⇒ DEFAULT_WARM_TABLES.
        max_workers: ThreadPoolExecutor size.
        target_rate_per_sec: aggregate API request rate ceiling.
        api_key: Census API key. None ⇒ env var CENSUS_API_KEY.
        log_path: where to write the warm log. None ⇒ default under cache_dir.
        dry_run: enumerate the work plan and exit without fetching.
        min_free_gb: halt if cache disk has less than this many GB free.

    Returns:
        WarmCacheResult.
    """
    cache_root = resolve_bulk_cache_dir(cache_dir)

    resolved_api_key = api_key or os.environ.get("CENSUS_API_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "warm_cache requires a Census API key: the bulk fetcher uses "
            "`group(...)` queries which the Census API rejects without a key "
            "(it redirects to an HTML missing-key page that breaks JSON parsing). "
            "Set CENSUS_API_KEY in the environment or pass api_key=... explicitly. "
            "Sign up at https://api.census.gov/data/key_signup.html"
        )

    free_gb = shutil.disk_usage(cache_root).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"Cache disk has {free_gb:.1f} GB free; need at least {min_free_gb} GB. "
            "Free space or pass a different cache_dir."
        )

    if log_path is None:
        log_path = cache_root / f"warm_log_{int(time.time())}.txt"

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logger.addHandler(file_handler)
    prev_level = logger.level
    if prev_level == logging.NOTSET:
        logger.setLevel(logging.INFO)

    try:
        return _warm_cache_impl(
            cache_root=cache_root,
            vintages_5yr=list(DEFAULT_WARM_VINTAGES_5YR) if vintages_5yr is None else list(vintages_5yr),
            vintages_1yr=list(DEFAULT_WARM_VINTAGES_1YR) if vintages_1yr is None else list(vintages_1yr),
            geographies_5yr=list(DEFAULT_WARM_GEOGRAPHIES_5YR) if geographies_5yr is None else list(geographies_5yr),
            geographies_1yr=list(DEFAULT_WARM_GEOGRAPHIES_1YR) if geographies_1yr is None else list(geographies_1yr),
            states=sorted(set(STATE_FIPS.values())) if states is None else list(states),
            tables=list(DEFAULT_WARM_TABLES) if tables is None else list(tables),
            max_workers=max_workers,
            target_rate_per_sec=target_rate_per_sec,
            api_key=resolved_api_key,
            log_path=log_path,
            dry_run=dry_run,
        )
    finally:
        logger.removeHandler(file_handler)
        file_handler.close()


def _warm_cache_impl(
    cache_root: Path,
    vintages_5yr: list[str],
    vintages_1yr: list[int],
    geographies_5yr: list[str],
    geographies_1yr: list[str],
    states: list[str],
    tables: list[str],
    max_workers: int,
    target_rate_per_sec: float,
    api_key: Optional[str],
    log_path: Path,
    dry_run: bool,
) -> WarmCacheResult:
    invalid_5yr_geo = [g for g in geographies_5yr if g not in DEFAULT_WARM_GEOGRAPHIES_5YR]
    invalid_1yr_geo = [g for g in geographies_1yr if g not in DEFAULT_WARM_GEOGRAPHIES_1YR]
    if invalid_5yr_geo or invalid_1yr_geo:
        raise ValueError(
            f"Invalid geographies: 5yr={invalid_5yr_geo} 1yr={invalid_1yr_geo}. "
            f"Allowed 5yr={DEFAULT_WARM_GEOGRAPHIES_5YR}, 1yr={DEFAULT_WARM_GEOGRAPHIES_1YR}"
        )

    all_tuples = _enumerate_tuples(
        vintages_5yr=vintages_5yr, vintages_1yr=vintages_1yr,
        geographies_5yr=geographies_5yr, geographies_1yr=geographies_1yr,
        states=states, tables=tables,
    )

    pending: list[_WarmTuple] = []
    skipped = 0
    for t in all_tuples:
        if bulk_parquet_path(cache_root, t.geography, t.vintage_tag, t.table, t.state_fips).exists():
            skipped += 1
        else:
            pending.append(t)

    logger.info(
        "Warm plan: %d total tuples; %d already cached (skip); %d to fetch.",
        len(all_tuples), skipped, len(pending),
    )
    _log_plan_summary(all_tuples, pending)

    if dry_run:
        return WarmCacheResult(
            completed_count=0, skipped_count=skipped, failed_count=0,
            total_duration_sec=0.0, failed_tuples=[], log_path=log_path,
        )

    rate_limiter = _RateLimiter(target_rate_per_sec)
    started = time.monotonic()
    completed = 0
    failed: list[tuple[_WarmTuple, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_process_tuple, t, cache_root, rate_limiter, api_key): t
            for t in pending
        }
        for fut in as_completed(futures):
            status, t, err = fut.result()
            if status == "completed":
                completed += 1
                if completed % 100 == 0:
                    logger.info(
                        "Progress: %d/%d completed, %d failed so far.",
                        completed, len(pending), len(failed),
                    )
            elif status == "failed":
                failed.append((t, err or ""))
                logger.error(
                    "FAILED %s/%s/%s_%s: %s",
                    t.geography, t.vintage_tag, t.table, t.state_fips, err,
                )

    # End-of-run retry pass on failed tuples (skip permanent 4xx-marked failures).
    retryable = [(t, e) for (t, e) in failed if not e.startswith("4") or e.startswith("429")]
    if retryable:
        logger.info("Retry pass for %d failed tuples with longer backoff.", len(retryable))
        still_failed: list[tuple[_WarmTuple, str]] = []
        rate_limiter_retry = _RateLimiter(target_rate_per_sec / 2)
        with ThreadPoolExecutor(max_workers=max(1, max_workers // 2)) as pool:
            futures = {
                pool.submit(
                    _process_tuple, t, cache_root, rate_limiter_retry, api_key,
                    {"max_attempts": 5, "base_delay": 5.0, "max_delay": 120.0},
                ): t for (t, _) in retryable
            }
            for fut in as_completed(futures):
                status, t, err = fut.result()
                if status == "completed":
                    completed += 1
                elif status == "failed":
                    still_failed.append((t, err or ""))

        permanent = [(t, e) for (t, e) in failed if e.startswith("4") and not e.startswith("429")]
        failed = permanent + still_failed

    duration = time.monotonic() - started
    logger.info(
        "Warm complete: %d completed, %d skipped, %d failed in %.1fs.",
        completed, skipped, len(failed), duration,
    )
    return WarmCacheResult(
        completed_count=completed,
        skipped_count=skipped,
        failed_count=len(failed),
        total_duration_sec=duration,
        failed_tuples=[(t.geography, t.vintage_tag, t.table, t.state_fips, e) for (t, e) in failed],
        log_path=log_path,
    )


def _log_plan_summary(all_tuples: list[_WarmTuple], pending: list[_WarmTuple]) -> None:
    """Print a per-(acs_type, geography) breakdown to the log."""
    from collections import Counter
    breakdown: Counter = Counter((t.acs_type, t.geography) for t in pending)
    if breakdown:
        for (acs_type, geo), n in sorted(breakdown.items()):
            logger.info("  pending %s %s: %d", acs_type, geo, n)
