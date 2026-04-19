"""
Parquet cache conventions.

Central filename scheme and cache directory resolution. Parquet files include
vintage in the filename so multiple vintages coexist without manual
invalidation.

Cache directory resolution (first match wins):
  1. explicit `cache_dir` argument passed to fetch function
  2. `TELLTALERE_CENSUS_CACHE_DIR` env var
  3. `./data/` relative to process CWD
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Optional


ENV_CACHE_DIR = "TELLTALERE_CENSUS_CACHE_DIR"


def resolve_cache_dir(explicit: Optional[Path] = None) -> Path:
    """Resolve the parquet cache directory, creating it if needed."""
    if explicit is not None:
        p = Path(explicit)
    elif os.environ.get(ENV_CACHE_DIR):
        p = Path(os.environ[ENV_CACHE_DIR])
    else:
        p = Path.cwd() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def package_data_path(filename: str) -> Path:
    """
    Return a filesystem path to a file shipped inside telltalere_census/data/.

    Uses importlib.resources so the lookup works whether the package is
    installed editable or as a wheel.
    """
    pkg_data = resources.files("telltalere_census") / "data" / filename
    # `resources.files` returns a Traversable; when the package is on-disk
    # (editable or unzipped), it yields a concrete pathlib.Path via `as_posix`.
    # For simplicity we assume on-disk install (the install pattern).
    return Path(str(pkg_data))


# ---------------------------------------------------------------------------
# Filename conventions
# ---------------------------------------------------------------------------

def pums_h_filename(acs_type: str, acs_year: int, state_fips: str) -> str:
    return f"pums_{acs_type}_{acs_year}_{state_fips}_H.parquet"


def pums_p_filename(acs_type: str, acs_year: int, state_fips: str) -> str:
    return f"pums_{acs_type}_{acs_year}_{state_fips}_P.parquet"


def bg_acs5_filename(acs_year: int, state_fips: str) -> str:
    return f"acs5_{acs_year}_{state_fips}_bg.parquet"


def tract_pop_filename(state_fips: str) -> str:
    return f"tract_pop_2020_{state_fips}.parquet"
