"""
County name / FIPS resolution via the Census geography API.

Domain-neutral: given either a "County Name, ST" string or a 5-digit FIPS,
returns the split state / county FIPS and a display name suitable for
downstream product output.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import requests

from telltalere_census.variables import STATE_FIPS, STATE_NAMES, FIPS_TO_ABBR


def resolve_county(
    county_name: Optional[str] = None,
    fips: Optional[str] = None,
    acs_year: int = 2024,
) -> tuple[str, str, str, str]:
    """
    Resolve to (state_fips_2, county_fips_3, county_fips_5, county_display_name).

    Exactly one of `county_name` or `fips` must be provided.

    `county_name` format: "Marion County, IN" — split by comma, state part may
    be a 2-letter abbreviation or a full state name.

    `fips` may be given as an int or a string (zero-padded to 5 digits).

    If CENSUS_API_KEY is set in the environment it is included in lookups;
    otherwise the Census public rate limit applies.
    """
    census_key = os.environ.get("CENSUS_API_KEY", "")

    if fips:
        fips = str(fips).zfill(5)
        st = fips[:2]
        co = fips[2:]
        abbr = FIPS_TO_ABBR.get(st, st)
        display_name = f"County {co}, {abbr}"
        try:
            url = (f"https://api.census.gov/data/{acs_year}/acs/acs5"
                   f"?get=NAME&for=county:{co}&in=state:{st}")
            if census_key:
                url += f"&key={census_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if len(rows) > 1:
                    display_name = rows[1][0]
        except Exception:
            pass
        return st, co, fips, display_name

    if county_name:
        parts = [p.strip() for p in county_name.split(",")]
        if len(parts) != 2:
            sys.exit(f"County format should be 'County Name, ST' — got: {county_name}")
        name_part, state_part = parts
        state_part = state_part.strip().upper()

        if state_part in STATE_FIPS:
            st_fips = STATE_FIPS[state_part]
        elif state_part in STATE_NAMES:
            st_fips = STATE_NAMES[state_part]
        else:
            sys.exit(f"Unknown state: {state_part}")

        url = (f"https://api.census.gov/data/{acs_year}/acs/acs5"
               f"?get=NAME&for=county:*&in=state:{st_fips}")
        if census_key:
            url += f"&key={census_key}"
        resp = requests.get(url)
        resp.raise_for_status()
        rows = resp.json()
        name_lower = name_part.lower()
        for row in rows[1:]:
            if name_lower in row[0].lower():
                co_fips = row[2]
                full_fips = st_fips + co_fips
                return st_fips, co_fips, full_fips, row[0]

        sys.exit(f"County '{name_part}' not found in state {state_part}")

    sys.exit("Must provide --county or --fips")
