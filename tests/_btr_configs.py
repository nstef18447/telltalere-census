"""
Test-internal BTR-flavored configurations for compose / e2e tests.

NOT a public package surface. Lives under tests/ specifically so it
doesn't leak BTR concepts into the package's import boundary (per the
session 3 + session 4 design intent: bin schemes belong to consumers,
not the package).

Provides:
  - BracketConfigs for renter and owner sides of B25118 (income),
    B25007 (age), B25009 (HH size).
  - Scalar PUMS binners that map continuous HINCP / AGEP / NP values
    into the same display bins. Used to pre-bin PUMS before calling
    `compute_puma_joint`.
  - The display bin-id ordering for income, age, HH size.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from telltalere_census import BracketConfig


# ---------------------------------------------------------------------------
# Display bin orderings
# ---------------------------------------------------------------------------

INCOME_BIN_IDS = ["under_35k", "35_50k", "50_75k", "75_100k", "100_150k", "150k_plus"]
AGE_BIN_IDS = ["under_25", "25_34", "35_44", "45_54", "55_64", "65_plus"]
HH_SIZE_BIN_IDS = ["1", "2", "3", "4", "5_plus"]


# ---------------------------------------------------------------------------
# B25118 — household income (owner brackets _003E.._013E, renter _015E.._025E)
# ---------------------------------------------------------------------------

_INCOME_RENTER_TO_DISPLAY: dict[str, str] = {
    "B25118_015E": "under_35k",   # < $5k
    "B25118_016E": "under_35k",   # $5-10k
    "B25118_017E": "under_35k",   # $10-15k
    "B25118_018E": "under_35k",   # $15-20k
    "B25118_019E": "under_35k",   # $20-25k
    "B25118_020E": "under_35k",   # $25-35k
    "B25118_021E": "35_50k",
    "B25118_022E": "50_75k",
    "B25118_023E": "75_100k",
    "B25118_024E": "100_150k",
    "B25118_025E": "150k_plus",
}

_INCOME_OWNER_TO_DISPLAY: dict[str, str] = {
    "B25118_003E": "under_35k",
    "B25118_004E": "under_35k",
    "B25118_005E": "under_35k",
    "B25118_006E": "under_35k",
    "B25118_007E": "under_35k",
    "B25118_008E": "under_35k",
    "B25118_009E": "35_50k",
    "B25118_010E": "50_75k",
    "B25118_011E": "75_100k",
    "B25118_012E": "100_150k",
    "B25118_013E": "150k_plus",
}

INCOME_RENTER_CONFIG: BracketConfig = {
    "denominator_var": "B25118_014E",
    "bracket_to_bin": _INCOME_RENTER_TO_DISPLAY,
    "bin_order": INCOME_BIN_IDS,
}

INCOME_OWNER_CONFIG: BracketConfig = {
    "denominator_var": "B25118_002E",
    "bracket_to_bin": _INCOME_OWNER_TO_DISPLAY,
    "bin_order": INCOME_BIN_IDS,
}


# ---------------------------------------------------------------------------
# B25007 — age of householder (owner _003E.._011E, renter _013E.._021E)
# ---------------------------------------------------------------------------

_AGE_RENTER_TO_DISPLAY: dict[str, str] = {
    "B25007_013E": "under_25",
    "B25007_014E": "25_34",
    "B25007_015E": "35_44",
    "B25007_016E": "45_54",
    "B25007_017E": "55_64",   # 55-59
    "B25007_018E": "55_64",   # 60-64
    "B25007_019E": "65_plus", # 65-74
    "B25007_020E": "65_plus", # 75-84
    "B25007_021E": "65_plus", # 85+
}

_AGE_OWNER_TO_DISPLAY: dict[str, str] = {
    "B25007_003E": "under_25",
    "B25007_004E": "25_34",
    "B25007_005E": "35_44",
    "B25007_006E": "45_54",
    "B25007_007E": "55_64",   # 55-59
    "B25007_008E": "55_64",   # 60-64
    "B25007_009E": "65_plus", # 65-74
    "B25007_010E": "65_plus", # 75-84
    "B25007_011E": "65_plus", # 85+
}

AGE_RENTER_CONFIG: BracketConfig = {
    "denominator_var": "B25007_012E",
    "bracket_to_bin": _AGE_RENTER_TO_DISPLAY,
    "bin_order": AGE_BIN_IDS,
}

AGE_OWNER_CONFIG: BracketConfig = {
    "denominator_var": "B25007_002E",
    "bracket_to_bin": _AGE_OWNER_TO_DISPLAY,
    "bin_order": AGE_BIN_IDS,
}


# ---------------------------------------------------------------------------
# B25009 — household size (owner _003E.._009E, renter _011E.._017E)
# ---------------------------------------------------------------------------

_HH_RENTER_TO_DISPLAY: dict[str, str] = {
    "B25009_011E": "1",
    "B25009_012E": "2",
    "B25009_013E": "3",
    "B25009_014E": "4",
    "B25009_015E": "5_plus",
    "B25009_016E": "5_plus",
    "B25009_017E": "5_plus",
}

_HH_OWNER_TO_DISPLAY: dict[str, str] = {
    "B25009_003E": "1",
    "B25009_004E": "2",
    "B25009_005E": "3",
    "B25009_006E": "4",
    "B25009_007E": "5_plus",
    "B25009_008E": "5_plus",
    "B25009_009E": "5_plus",
}

HH_RENTER_CONFIG: BracketConfig = {
    "denominator_var": "B25009_010E",
    "bracket_to_bin": _HH_RENTER_TO_DISPLAY,
    "bin_order": HH_SIZE_BIN_IDS,
}

HH_OWNER_CONFIG: BracketConfig = {
    "denominator_var": "B25009_002E",
    "bracket_to_bin": _HH_OWNER_TO_DISPLAY,
    "bin_order": HH_SIZE_BIN_IDS,
}


# ---------------------------------------------------------------------------
# Scalar PUMS binners — map continuous values into display bins.
# ---------------------------------------------------------------------------

def bin_income(hincp: Optional[float]) -> Optional[str]:
    """HINCP -> display bin id. Negative HINCP (loss) bins to under_35k."""
    if hincp is None or pd.isna(hincp):
        return None
    v = float(hincp)
    if v < 35_000:
        return "under_35k"
    if v < 50_000:
        return "35_50k"
    if v < 75_000:
        return "50_75k"
    if v < 100_000:
        return "75_100k"
    if v < 150_000:
        return "100_150k"
    return "150k_plus"


def bin_age(agep: Optional[float]) -> Optional[str]:
    """AGEP -> display bin id. AGEP < 15 (non-householder) returns None."""
    if agep is None or pd.isna(agep):
        return None
    v = float(agep)
    if v < 15:
        return None
    if v < 25:
        return "under_25"
    if v < 35:
        return "25_34"
    if v < 45:
        return "35_44"
    if v < 55:
        return "45_54"
    if v < 65:
        return "55_64"
    return "65_plus"


def bin_hh_size(np_: Optional[float]) -> Optional[str]:
    """NP -> display bin id. Clips at 5+."""
    if np_ is None or pd.isna(np_):
        return None
    v = int(round(float(np_)))
    if v < 1:
        return None
    if v >= 5:
        return "5_plus"
    return str(v)
