"""
Domain-neutral derived columns computed from PUMS microdata.

These fields do not depend on any product-specific concept (AMI, HUD limits,
CoStar, Zillow, BTR thresholds) — they are bucketings and recodes of raw
PUMS variables that any downstream product would plausibly want.

Fields that DO depend on AMI (e.g. `choice_renter`, `affordability_trap`,
`below_market_indicator`) live in ami-tool's product-local
`ami_derived_fields.py`.
"""

from __future__ import annotations

import pandas as pd

from telltalere_census.variables import (
    BLD_TO_STRUCTURE,
    BUILDING_ERA_BINS,
    HHT_TO_TYPE,
    AGE_COHORT_BINS,
)


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all domain-neutral derived columns to the DataFrame (returns a copy).

    Adds:
        tenure, cost_burden, age_cohort, building_era, structure_type,
        bedroom_tier, household_type_simple,
        moved_12mo, moved_24mo, tenure_bucket, turnover_signal,
        mover_type, has_children, vehicle_access, employment_status,
        education_level, snap_recipient, broadband_access, poverty_flag

    Callers that need AMI-dependent fields should run product-specific
    derived-field logic after this.
    """
    df = df.copy()

    # 0. Tenure classification (needed before cost_burden).
    # TEN: 1=owned w/mortgage, 2=owned free, 3=rented, 4=no cash rent.
    # String values preserved exactly for backward compatibility: "Owner"/"Renter".
    if "TEN" in df.columns:
        df["tenure"] = df["TEN"].map({1: "Owner", 2: "Owner", 3: "Renter", 4: "Renter"})
    else:
        df["tenure"] = pd.NA

    # 1. Cost burden (renters only: GRNTP / monthly income)
    df["cost_burden"] = _compute_cost_burden(df)

    # 2. Age cohort (from householder AGEP)
    df["age_cohort"] = _compute_age_cohort(df)

    # 3. Building era (from YRBLT — actual year values)
    df["building_era"] = _compute_building_era(df)

    # 4. Structure type (from BLD)
    df["structure_type"] = df["BLD"].map(BLD_TO_STRUCTURE) if "BLD" in df.columns else pd.NA

    # 5. Bedroom tier
    df["bedroom_tier"] = _compute_bedroom_tier(df)

    # 6. Household type simplified
    df["household_type_simple"] = df["HHT"].map(HHT_TO_TYPE) if "HHT" in df.columns else pd.NA

    # 7. Turnover flags (from MV — "when moved into this unit")
    # MV coding: 1=<=12 months, 2=13-23 months, 3=2-4 yrs, 4=5-9 yrs,
    #            5=10-19 yrs, 6=20-29 yrs, 7=30+ yrs
    df["moved_12mo"] = False
    df["moved_24mo"] = False
    if "MV" in df.columns:
        df["moved_12mo"] = df["MV"] == 1
        df["moved_24mo"] = df["MV"].isin([1, 2])

    # 8. Tenure bucket (how long in current unit)
    df["tenure_bucket"] = "unknown"
    if "MV" in df.columns:
        df.loc[df["MV"] == 1, "tenure_bucket"] = "new"
        df.loc[df["MV"] == 2, "tenure_bucket"] = "recent"
        df.loc[df["MV"] == 3, "tenure_bucket"] = "established"
        df.loc[df["MV"] == 4, "tenure_bucket"] = "long_term"
        df.loc[df["MV"].isin([5, 6, 7]), "tenure_bucket"] = "entrenched"

    # 9. Turnover signal (aggregated stability indicator)
    df["turnover_signal"] = "unknown"
    if "MV" in df.columns:
        df.loc[df["MV"].isin([1, 2]), "turnover_signal"] = "high_turnover"
        df.loc[df["MV"] == 3, "turnover_signal"] = "stable"
        df.loc[df["MV"].isin([4, 5, 6, 7]), "turnover_signal"] = "locked_in"

    # 10. Mover origin (from MIG)
    # MIG coding: 1=same house, 2=from abroad, 3=different house in US
    df["mover_type"] = pd.NA
    if "MIG" in df.columns:
        df.loc[df["MIG"] == 1, "mover_type"] = "non_mover"
        df.loc[df["MIG"] == 3, "mover_type"] = "domestic_mover"
        df.loc[df["MIG"] == 2, "mover_type"] = "from_abroad"

    # 11. Has children flag
    df["has_children"] = False
    if "HUPAC" in df.columns:
        df["has_children"] = df["HUPAC"].isin([1, 2, 3])

    # 12. Vehicle access (from VEH)
    df["vehicle_access"] = "unknown"
    if "VEH" in df.columns:
        df.loc[df["VEH"] == 0, "vehicle_access"] = "none"
        df.loc[df["VEH"] == 1, "vehicle_access"] = "one"
        df.loc[df["VEH"] >= 2, "vehicle_access"] = "two_plus"

    # 13. Employment status (from ESR)
    # ESR: 1=civilian employed at work, 2=civilian employed with job not at work,
    #      3=unemployed, 4=armed forces at work, 5=armed forces with job not at work,
    #      6=not in labor force. N/A (under 16) comes through as <NA>.
    df["employment_status"] = "unknown"
    if "ESR" in df.columns:
        df.loc[df["ESR"].isin([1, 2]), "employment_status"] = "employed"
        df.loc[df["ESR"] == 3, "employment_status"] = "unemployed"
        df.loc[df["ESR"] == 6, "employment_status"] = "not_in_labor_force"
        df.loc[df["ESR"].isin([4, 5]), "employment_status"] = "military"

    # 14. Education level (from SCHL)
    # SCHL: 01-15=less than HS, 16-17=HS/GED, 18-20=some college/assoc,
    #       21=bachelor's, 22-24=graduate/professional
    df["education_level"] = "unknown"
    if "SCHL" in df.columns:
        df.loc[(df["SCHL"] >= 1) & (df["SCHL"] <= 15), "education_level"] = "less_than_hs"
        df.loc[df["SCHL"].isin([16, 17]), "education_level"] = "hs_or_ged"
        df.loc[df["SCHL"].isin([18, 19, 20]), "education_level"] = "some_college"
        df.loc[df["SCHL"] == 21, "education_level"] = "bachelors"
        df.loc[df["SCHL"].isin([22, 23, 24]), "education_level"] = "graduate"

    # 15. SNAP recipient (from FS)
    # FS: 1=received food stamps/SNAP, 2=did not
    df["snap_recipient"] = False
    if "FS" in df.columns:
        df["snap_recipient"] = df["FS"] == 1

    # 16. Broadband access (from BROADBND)
    # BROADBND: 1=yes, 2=no
    df["broadband_access"] = False
    if "BROADBND" in df.columns:
        df["broadband_access"] = df["BROADBND"] == 1

    # 17. Poverty flag (from POVPIP)
    # POVPIP: income-to-poverty ratio x 100. E.g. 150 = 150% of poverty line.
    # Values 0-501 (501 = 501% or more). N/A comes through as <NA>.
    df["poverty_flag"] = "unknown"
    if "POVPIP" in df.columns:
        df.loc[df["POVPIP"] < 50, "poverty_flag"] = "deep_poverty"
        df.loc[(df["POVPIP"] >= 50) & (df["POVPIP"] < 100), "poverty_flag"] = "poverty"
        df.loc[(df["POVPIP"] >= 100) & (df["POVPIP"] < 200), "poverty_flag"] = "near_poverty"
        df.loc[df["POVPIP"] >= 200, "poverty_flag"] = "above_poverty"

    return df


def _compute_cost_burden(df: pd.DataFrame) -> pd.Series:
    """
    Cost burden = gross rent / monthly income.
    Only meaningful for renters with positive income and rent.
    Requires `tenure` to be populated on the input.
    """
    result = pd.Series(pd.NA, index=df.index, dtype="object")

    if "GRNTP" not in df.columns or "tenure" not in df.columns:
        return result

    mask = (
        (df["tenure"] == "Renter") &
        (df["HINCP"] > 0) &
        (df["GRNTP"].notna()) &
        (df["GRNTP"] > 0)
    )

    monthly_income = df.loc[mask, "HINCP"] / 12
    ratio = df.loc[mask, "GRNTP"] / monthly_income

    result.loc[mask & (ratio < 0.30)] = "none"
    result.loc[mask & (ratio >= 0.30) & (ratio < 0.50)] = "moderate"
    result.loc[mask & (ratio >= 0.50) & (ratio < 0.75)] = "severe"
    result.loc[mask & (ratio >= 0.75)] = "extreme"

    return result


def _compute_age_cohort(df: pd.DataFrame) -> pd.Series:
    """Bin householder age into cohorts."""
    result = pd.Series(pd.NA, index=df.index, dtype="object")
    if "AGEP" not in df.columns:
        return result
    for lo, hi, label in AGE_COHORT_BINS:
        mask = (df["AGEP"] >= lo) & (df["AGEP"] <= hi)
        result.loc[mask] = label
    return result


def _compute_building_era(df: pd.DataFrame) -> pd.Series:
    """Bin YRBLT (actual year) into era categories."""
    result = pd.Series(pd.NA, index=df.index, dtype="object")
    if "YRBLT" not in df.columns:
        return result
    for lo, hi, label in BUILDING_ERA_BINS:
        mask = (df["YRBLT"] >= lo) & (df["YRBLT"] <= hi)
        result.loc[mask] = label
    return result


def _compute_bedroom_tier(df: pd.DataFrame) -> pd.Series:
    """
    Recode BDSP into bedroom tiers.

    BDSP: 0=studio, 1=1BR, 2=2BR, 3=3BR, 4+=4BR+.
    5 buckets — studio and 1BR split for rent gap precision; 3BR and 4BR+
    split because they are distinct product types with meaningfully
    different family profiles and turnover rates.
    """
    result = pd.Series(pd.NA, index=df.index, dtype="object")

    if "BDSP" not in df.columns:
        return result

    result.loc[df["BDSP"] == 0] = "studio"
    result.loc[df["BDSP"] == 1] = "1br"
    result.loc[df["BDSP"] == 2] = "2br"
    result.loc[df["BDSP"] == 3] = "3br"
    result.loc[df["BDSP"] >= 4] = "4br_plus"

    return result
