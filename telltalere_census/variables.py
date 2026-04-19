"""
Variable code constants, default whitelists, and bucketing lookups for the
Census ACS PUMS and ACS5 summary data.

All constants here are frozen to modern vintages (2023+ for PUMS, 2022+ ACS5).
Do not edit without confirming against the current data dictionary.
"""

from typing import Final

# ---------------------------------------------------------------------------
# PUMA Crosswalk URL
# ---------------------------------------------------------------------------

CROSSWALK_URL: Final[str] = (
    "https://www2.census.gov/geo/docs/reference/puma2020/"
    "2020_Census_Tract_to_2020_PUMA.txt"
)

# ---------------------------------------------------------------------------
# State FIPS Lookups
# ---------------------------------------------------------------------------

STATE_FIPS: Final[dict[str, str]] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}

STATE_NAMES: Final[dict[str, str]] = {
    "ALABAMA": "01", "ALASKA": "02", "ARIZONA": "04", "ARKANSAS": "05",
    "CALIFORNIA": "06", "COLORADO": "08", "CONNECTICUT": "09", "DELAWARE": "10",
    "DISTRICT OF COLUMBIA": "11", "FLORIDA": "12", "GEORGIA": "13", "HAWAII": "15",
    "IDAHO": "16", "ILLINOIS": "17", "INDIANA": "18", "IOWA": "19", "KANSAS": "20",
    "KENTUCKY": "21", "LOUISIANA": "22", "MAINE": "23", "MARYLAND": "24",
    "MASSACHUSETTS": "25", "MICHIGAN": "26", "MINNESOTA": "27", "MISSISSIPPI": "28",
    "MISSOURI": "29", "MONTANA": "30", "NEBRASKA": "31", "NEVADA": "32",
    "NEW HAMPSHIRE": "33", "NEW JERSEY": "34", "NEW MEXICO": "35", "NEW YORK": "36",
    "NORTH CAROLINA": "37", "NORTH DAKOTA": "38", "OHIO": "39", "OKLAHOMA": "40",
    "OREGON": "41", "PENNSYLVANIA": "42", "RHODE ISLAND": "44", "SOUTH CAROLINA": "45",
    "SOUTH DAKOTA": "46", "TENNESSEE": "47", "TEXAS": "48", "UTAH": "49",
    "VERMONT": "50", "VIRGINIA": "51", "WASHINGTON": "53", "WEST VIRGINIA": "54",
    "WISCONSIN": "55", "WYOMING": "56", "PUERTO RICO": "72",
}

FIPS_TO_ABBR: Final[dict[str, str]] = {v: k for k, v in STATE_FIPS.items()}


# ---------------------------------------------------------------------------
# PUMS Variable Lists — Housing (H) and Person (P) defaults
# ---------------------------------------------------------------------------
# Confirmed against 2024 ACS 5-year PUMS data dictionary (2024-04-11).
# The Census API separates H and P records automatically based on the
# variables requested. SERIALNO links them.
#
# Variable name notes:
#   YRBLT — year built (actual years). YBL does NOT exist in 2024 ACS5.
#   RELSHIPP — relationship to householder. RELP does NOT exist in 2024 ACS5.
#   PUMA — 2024 ACS5 uses PUMA. Only older ACS5 (<=2022) used PUMA20.
#   MV — string "1"-"7" from the API. Coded: 1=<=12mo, 2=13-23mo, 3=2-4yr,
#         4=5-9yr, 5=10-19yr, 6=20-29yr, 7=30+yr.

PUMS_DEFAULT_HOUSING_VARS: Final[list[str]] = [
    # Core identifiers and weights
    "SERIALNO", "WGTP",

    # Household economics
    "HINCP",      # household income (past 12 months)
    "NP",         # number of persons in unit
    "TEN",        # tenure (1=owned w/mortgage, 2=owned free, 3=rented, 4=no cash rent)
    "GRNTP",      # gross rent (monthly, including utilities)
    "RNTP",       # contract rent (monthly, without utilities)
    "VALP",       # property value (owners only)

    # Unit characteristics
    "BLD",        # units in structure (1-10 code)
    "YRBLT",      # year structure built (actual year, not coded)
    "BDSP",       # number of bedrooms
    "RMSP",       # number of rooms
    "VEH",        # vehicles available

    # Household composition (pre-computed by Census on H record)
    "HHT",        # household/family type
    "HUPAC",      # presence and age of children
    "NOC",        # number of own children
    "NPF",        # number of persons in family
    "R18",        # presence of persons under 18 in household
    "R65",        # presence of persons 65+ in household
    "WIF",        # workers in family
    "MULTG",      # multigenerational household flag
    "LNGI",       # limited English speaking household

    # Mobility and tenure
    "MV",         # when moved into unit (1-7 tenure scale)

    # Financial assistance
    "FS",         # food stamp / SNAP receipt (1=yes, 2=no)

    # Internet access
    "BROADBND",   # broadband subscription
    "HISPEED",    # high-speed internet service

    # Poverty
    "POVPIP",     # income to poverty ratio (0-501+, string from API)
]

PUMS_DEFAULT_PERSON_VARS: Final[list[str]] = [
    # Identifiers
    "SERIALNO",
    "RELSHIPP",   # relationship to householder — filter to == '20'

    # Demographics
    "AGEP",       # age
    "SEX",        # sex (1=male, 2=female)

    # Migration
    "MIG",        # moved in last 12 months (1=same house, 2=abroad, 3=diff US house)

    # Education and employment
    "SCHG",       # school grade enrollment (15=college, 16=grad/professional)
    "SCHL",       # educational attainment (01-24 code)
    "ESR",        # employment status recode
    "COW",        # class of worker
    "NAICSP",     # industry (NAICS code, string)
    "OCCP",       # occupation code (string)

    # Income (person-level)
    "WAGP",       # wages and salary income
    "PINCP",      # total person income
    "SSIP",       # supplemental security income

    # Health and disability
    "DIS",        # disability status (1=with disability, 2=without)
    "HICOV",      # health insurance coverage (1=yes, 2=no)

    # Background
    "POBP",       # place of birth (code)
    "CIT",        # citizenship status (1-5 code)
    "LANX",       # language other than English at home (1=yes, 2=no)
]


# ---------------------------------------------------------------------------
# ACS5 Block-Group Summary Table Defaults
# ---------------------------------------------------------------------------
# Starter set: total population, household count, median household income,
# median gross rent. Caller typically extends this with table-specific
# variables (B25034 vintage, B19013 income detail, etc.).

ACS_BG_DEFAULT_VARS: Final[list[str]] = [
    "B01003_001E",   # total population
    "B11001_001E",   # total households
    "B19013_001E",   # median household income (dollars)
    "B25064_001E",   # median gross rent (dollars)
    "B25003_002E",   # owner-occupied households
    "B25003_003E",   # renter-occupied households
]


# ---------------------------------------------------------------------------
# BLD (Units in Structure) Recode → structure_type
# ---------------------------------------------------------------------------

BLD_TO_STRUCTURE: Final[dict[int, str]] = {
    1: "sfr", 2: "sfr",                   # SFR detached, attached
    3: "small_multifamily",                # 2 units
    4: "small_multifamily",                # 3-4 units
    5: "mid_multifamily",                  # 5-9 units
    6: "mid_multifamily",                  # 10-19 units
    7: "large_multifamily",                # 20-49 units
    8: "large_multifamily",                # 50+ units
    9: "other", 10: "other",               # mobile home, boat/RV/other
}

# ---------------------------------------------------------------------------
# YRBLT (Year Built) → building_era
# YRBLT returns actual year values (e.g. 1939, 1970, 2020).
# We bin into era categories.
# ---------------------------------------------------------------------------

BUILDING_ERA_BINS: Final[list[tuple[int, int, str]]] = [
    (0, 1959, "pre_1960"),
    (1960, 1979, "1960_1979"),
    (1980, 1999, "1980_1999"),
    (2000, 2009, "2000_2009"),
    (2010, 9999, "2010_plus"),
]

# ---------------------------------------------------------------------------
# HHT (Household Type) Recode → household_type_simple
# ---------------------------------------------------------------------------

HHT_TO_TYPE: Final[dict[int, str]] = {
    1: "married_couple",
    2: "single_parent", 3: "single_parent",
    4: "single_person", 6: "single_person",
    5: "roommates", 7: "roommates",
}

# ---------------------------------------------------------------------------
# Bedroom Tiers (canonical ordering)
# ---------------------------------------------------------------------------

BEDROOM_TIERS: Final[list[str]] = ["studio", "1br", "2br", "3br", "4br_plus"]

# ---------------------------------------------------------------------------
# Age Cohort Bins
# ---------------------------------------------------------------------------

AGE_COHORT_BINS: Final[list[tuple[int, int, str]]] = [
    (0, 24, "under_25"),
    (25, 34, "25_34"),
    (35, 44, "35_44"),
    (45, 54, "45_54"),
    (55, 61, "55_61"),
    (62, 74, "62_plus"),
    (75, 999, "75_plus"),
]
