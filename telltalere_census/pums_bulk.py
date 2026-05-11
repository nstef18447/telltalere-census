"""
Bulk PUMS download from the Census FTP.

The Census API offers PUMS microdata but is slow for full-state, all-variable
pulls. The Census FTP at `https://www2.census.gov/programs-surveys/acs/data/pums/`
publishes per-state ZIPs containing the full microdata (housing-unit and
person records) — multi-megabyte but downloaded once they replace dozens
of API calls.

URL pattern (verified 2026-05-11):
  https://www2.census.gov/programs-surveys/acs/data/pums/{end_year}/
    {5-Year|1-Year}/csv_{record_type}{state_abbrev_lower}.zip

Where:
  - end_year:      4-digit year (the END year of the 5-year period, or the
                   1-year year itself)
  - record_type:   "h" for housing-unit records, "p" for person records
  - state_abbrev:  2-letter postal abbreviation, lowercased (e.g. "il", "pr")

Each ZIP contains one CSV file. We extract the CSV, convert to parquet, and
write to:
  {cache_root}/pums/{vintage_tag}/{record_type}_{state_fips}.parquet

`vintage_tag` mirrors the ACS convention:
  - 5-year: "{start}-{end}", e.g. "2020-2024"
  - 1-year: str(year)

PUMS FTP has no documented rate limit; downloads are bandwidth-bound, not
request-count-bound. We run a small ThreadPoolExecutor (default 4 concurrent)
and let TCP/HTTP throttle.

Note on column count: 2020+ PUMS files have ~260 H columns and ~290 P
columns. We do not pre-type — pandas infers types on parquet write. Downstream
consumers can do typed reads via pyarrow if needed.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from telltalere_census.variables import FIPS_TO_ABBR
from telltalere_census.warm_cache import (
    DEFAULT_WARM_VINTAGES_1YR,
    DEFAULT_WARM_VINTAGES_5YR,
    parse_vintage_tag_5yr,
    resolve_bulk_cache_dir,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUMS_FTP_BASE = "https://www2.census.gov/programs-surveys/acs/data/pums"

DEFAULT_PUMS_RECORD_TYPES: tuple[str, ...] = ("h", "p")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PumsBulkResult:
    completed_count: int
    skipped_count: int
    failed_count: int
    total_duration_sec: float
    failed_tuples: list[tuple] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"PumsBulkResult(completed={self.completed_count}, "
            f"skipped={self.skipped_count}, failed={self.failed_count}, "
            f"duration={self.total_duration_sec:.1f}s)"
        )


# ---------------------------------------------------------------------------
# URL + path helpers
# ---------------------------------------------------------------------------

def _pums_url(end_year: int, pums_type: str, state_abbrev: str, record_type: str) -> str:
    """Build the FTP URL for one PUMS ZIP."""
    period_segment = "5-Year" if pums_type == "5yr" else "1-Year"
    return (
        f"{_PUMS_FTP_BASE}/{end_year}/{period_segment}/"
        f"csv_{record_type}{state_abbrev.lower()}.zip"
    )


def _pums_parquet_path(
    cache_root: Path, vintage_tag: str, record_type: str, state_fips: str,
) -> Path:
    return cache_root / "pums" / vintage_tag / f"{record_type}_{state_fips}.parquet"


def _vintage_tag_for(pums_type: str, vintage: int | str) -> str:
    if pums_type == "5yr":
        if isinstance(vintage, str):
            return vintage  # already a "2020-2024"-style tag
        return f"{int(vintage) - 4}-{int(vintage)}"
    return str(int(vintage))


def _end_year_for(pums_type: str, vintage: int | str) -> int:
    if pums_type == "5yr":
        if isinstance(vintage, str):
            return parse_vintage_tag_5yr(vintage)
        return int(vintage)
    return int(vintage)


# ---------------------------------------------------------------------------
# Single-download worker
# ---------------------------------------------------------------------------

def _download_one(
    end_year: int, pums_type: str, state_fips: str, record_type: str,
    cache_root: Path, vintage_tag: str, timeout: float = 600.0,
) -> tuple[str, dict, Optional[str]]:
    """Returns (status, descriptor, error). status in {completed, skipped, failed}."""
    target = _pums_parquet_path(cache_root, vintage_tag, record_type, state_fips)
    desc = {
        "vintage_tag": vintage_tag, "pums_type": pums_type,
        "state_fips": state_fips, "record_type": record_type,
    }
    if target.exists():
        return ("skipped", desc, None)

    state_abbrev = FIPS_TO_ABBR.get(state_fips)
    if not state_abbrev:
        return ("failed", desc, f"unknown state FIPS {state_fips}")
    url = _pums_url(end_year, pums_type, state_abbrev, record_type)

    try:
        resp = requests.get(url, timeout=timeout, stream=True)
    except requests.RequestException as e:
        return ("failed", desc, f"GET failed: {e}")
    if resp.status_code != 200:
        return ("failed", desc, f"HTTP {resp.status_code} from {url}")

    try:
        content = resp.content
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_members:
                return ("failed", desc, f"no CSV inside {url}")
            with zf.open(csv_members[0]) as fh:
                df = pd.read_csv(fh, low_memory=False)
    except (zipfile.BadZipFile, pd.errors.ParserError) as e:
        return ("failed", desc, f"unzip/parse failed: {e}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
    except Exception as e:  # noqa: BLE001
        return ("failed", desc, f"parquet write failed: {e}")

    return ("completed", desc, None)


# ---------------------------------------------------------------------------
# Enumerate work plan
# ---------------------------------------------------------------------------

def _enumerate_pums_jobs(
    vintages_5yr: list[str], vintages_1yr: list[int],
    states: list[str], record_types: list[str],
) -> list[dict]:
    out = []
    for vintage_tag in vintages_5yr:
        end_year = parse_vintage_tag_5yr(vintage_tag)
        for st in states:
            for rt in record_types:
                out.append({
                    "pums_type": "5yr", "end_year": end_year, "vintage_tag": vintage_tag,
                    "state_fips": st, "record_type": rt,
                })
    for year in vintages_1yr:
        for st in states:
            for rt in record_types:
                out.append({
                    "pums_type": "1yr", "end_year": year, "vintage_tag": str(year),
                    "state_fips": st, "record_type": rt,
                })
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def download_pums_bulk(
    cache_dir: Optional[Path] = None,
    vintages_5yr: Optional[list[str]] = None,
    vintages_1yr: Optional[list[int]] = None,
    pums_type: Optional[list[str]] = None,
    states: Optional[list[str]] = None,
    record_types: Optional[list[str]] = None,
    max_concurrent_downloads: int = 4,
    dry_run: bool = False,
    min_free_gb: float = 40.0,
) -> PumsBulkResult:
    """
    Bulk-download Census PUMS ZIPs from the FTP and convert each to parquet.

    Output: `{cache_root}/pums/{vintage_tag}/{h|p}_{state_fips}.parquet`.

    Args:
        cache_dir: override bulk cache root.
        vintages_5yr: 5-year bulk vintage tags. None ⇒ DEFAULT_WARM_VINTAGES_5YR.
        vintages_1yr: 1-year vintage years (int). None ⇒ DEFAULT_WARM_VINTAGES_1YR.
        pums_type: which PUMS products to include, subset of ["5yr", "1yr"].
            None ⇒ both. If "5yr" excluded, vintages_5yr is ignored.
        states: 2-digit state FIPS list. None ⇒ all 52 (50 + DC + PR).
        record_types: subset of ["h", "p"]. None ⇒ both.
        max_concurrent_downloads: ThreadPoolExecutor size for downloads.
        dry_run: enumerate and exit without downloading.
        min_free_gb: halt if cache disk has less free space than this.

    Returns:
        PumsBulkResult.
    """
    from telltalere_census.variables import STATE_FIPS

    cache_root = resolve_bulk_cache_dir(cache_dir)
    free_gb = shutil.disk_usage(cache_root).free / (1024 ** 3)
    if free_gb < min_free_gb:
        raise RuntimeError(
            f"Cache disk has {free_gb:.1f} GB free; need at least {min_free_gb} GB. "
            "Free space or pass a different cache_dir."
        )

    types = list(pums_type) if pums_type is not None else ["5yr", "1yr"]
    v5_default = list(DEFAULT_WARM_VINTAGES_5YR) if vintages_5yr is None else list(vintages_5yr)
    v1_default = list(DEFAULT_WARM_VINTAGES_1YR) if vintages_1yr is None else list(vintages_1yr)
    v5 = v5_default if "5yr" in types else []
    v1 = v1_default if "1yr" in types else []
    state_list = sorted(set(STATE_FIPS.values())) if states is None else list(states)
    rts = list(DEFAULT_PUMS_RECORD_TYPES) if record_types is None else list(record_types)

    jobs = _enumerate_pums_jobs(v5, v1, state_list, rts)

    pending = []
    skipped = 0
    for j in jobs:
        if _pums_parquet_path(cache_root, j["vintage_tag"], j["record_type"], j["state_fips"]).exists():
            skipped += 1
        else:
            pending.append(j)

    logger.info(
        "PUMS plan: %d jobs total, %d already cached, %d to download.",
        len(jobs), skipped, len(pending),
    )

    if dry_run:
        return PumsBulkResult(
            completed_count=0, skipped_count=skipped, failed_count=0,
            total_duration_sec=0.0, failed_tuples=[],
        )

    started = time.monotonic()
    completed = 0
    failed: list[tuple] = []

    with ThreadPoolExecutor(max_workers=max_concurrent_downloads) as pool:
        futures = {
            pool.submit(
                _download_one,
                j["end_year"], j["pums_type"], j["state_fips"],
                j["record_type"], cache_root, j["vintage_tag"],
            ): j for j in pending
        }
        for fut in as_completed(futures):
            status, desc, err = fut.result()
            if status == "completed":
                completed += 1
                if completed % 20 == 0:
                    logger.info(
                        "PUMS progress: %d/%d completed, %d failed.",
                        completed, len(pending), len(failed),
                    )
            elif status == "failed":
                failed.append((desc, err))
                logger.error(
                    "PUMS FAILED %s/%s/%s_%s: %s",
                    desc["pums_type"], desc["vintage_tag"],
                    desc["record_type"], desc["state_fips"], err,
                )

    duration = time.monotonic() - started
    logger.info(
        "PUMS complete: %d completed, %d skipped, %d failed in %.1fs.",
        completed, skipped, len(failed), duration,
    )
    return PumsBulkResult(
        completed_count=completed, skipped_count=skipped,
        failed_count=len(failed), total_duration_sec=duration,
        failed_tuples=[(d, e) for (d, e) in failed],
    )
