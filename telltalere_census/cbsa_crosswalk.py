"""
County ↔ CBSA crosswalk loader.

The Census CBSA delineation file parsing (which is ultimately how
`county_to_cbsa.parquet` gets produced) currently lives in ami-tool's
`costar_fetch.py` because it is tightly coupled to that product's CoStar
market mapping. Only the lookup side is exposed here so downstream products
can consult the crosswalk without pulling in CoStar logic.

If a second consumer needs to rebuild the crosswalk from the Census XLSX,
extract `costar_fetch._ensure_cbsa_xlsx` / `load_cbsa_crosswalk` at that
point. Flagged in REFACTOR_NOTES.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from telltalere_census.cache import package_data_path


def load_county_to_cbsa(path: Optional[Path] = None) -> pd.DataFrame:
    """
    Load the county → CBSA crosswalk table.

    Resolution order:
      1. explicit `path` argument
      2. shipped package data at telltalere_census/data/county_to_cbsa.parquet

    Returns:
        DataFrame with county_fips-keyed CBSA mappings (columns vary — use
        the file directly). Typical columns include `county_fips`,
        `cbsa_code`, `cbsa_title`.
    """
    if path is not None:
        return pd.read_parquet(Path(path))
    return pd.read_parquet(package_data_path("county_to_cbsa.parquet"))


def get_cbsa_for_county(county_fips: str, crosswalk: Optional[pd.DataFrame] = None) -> Optional[str]:
    """Return the CBSA code for a county FIPS, or None if not in any MSA."""
    if crosswalk is None:
        crosswalk = load_county_to_cbsa()
    match = crosswalk[crosswalk["county_fips"] == str(county_fips).zfill(5)]
    if match.empty:
        return None
    return str(match.iloc[0].get("cbsa_code"))
