"""
Tract-level (or block-group-level) marginal-distribution computation from
ACS bracket-segmented summary tables.

A "marginal" here is a 1-D distribution along one demographic axis (e.g.
income, age, household size) for one universe slice (e.g. renter-occupied
or owner-occupied). The function takes a single tract row from
`acs_fetch.fetch_acs_data` output, plus a `BracketConfig` describing which
ACS variables to roll up and into what display bins, and returns a
{bin_id: count} dict.

Tenure-/bin-agnostic by design — the package supplies the mechanism only.
The bracket configurations themselves (e.g. B25118 renter income →
INCOME_BINS) live in consumer projects (Core_BTR, demographics inspector).

The companion helper `subtotal_moe_flagged` answers "is this tract's
estimate of the relevant subtotal reliably published?" It checks the CV
(MOE / estimate) of a single subtotal cell against a threshold (default
0.40). Bracket-level CVs are wide on small tracts almost by construction
(noise scales as 1/sqrt(N)), so flagging on a per-bracket CV produces
many false positives; flagging on the subtotal CV answers the question
the UI actually needs to display.

Authoritative-total option: when `authoritative_total_var` is provided,
the total household count is read from that variable rather than computed
as the sum of bin contributions. This is recommended for tenure totals
(B25003_002E for owner, B25003_003E for renter) which are less subject
to suppression than per-table subtotal cells. The returned dict still
sums to the *bin contributions* — the authoritative total is returned
as a separate value via `compute_tract_marginal_with_total` if you need
both pieces.
"""

from __future__ import annotations

from typing import Optional, TypedDict

import pandas as pd


DEFAULT_MOE_CV_THRESHOLD = 0.40


class BracketConfig(TypedDict):
    """
    Configuration describing how to roll up an ACS table's bracket cells
    into display bins.

    Attributes:
        denominator_var: ACS variable code for the table's relevant
            subtotal (e.g. "B25118_014E" — renter-occupied subtotal in the
            B25118 income table). Used by `subtotal_moe_flagged` and as
            an optional sanity-check denominator.
        bracket_to_bin: mapping from each bracket variable code to the
            display bin id it contributes to. Multiple codes may target
            the same bin — counts are summed.
        bin_order: ordered list of all display bin ids. Defines the
            iteration / display order; every id appears in the output
            dict regardless of whether any contribution was made.
    """

    denominator_var: str
    bracket_to_bin: dict[str, str]
    bin_order: list[str]


def compute_tract_marginal(
    row: pd.Series,
    config: BracketConfig,
) -> dict[str, int]:
    """
    Compute a single 1-D marginal distribution from a tract row.

    Sums each bracket cell named in `config["bracket_to_bin"]` into its
    target bin. Suppressed cells (NaN / None) contribute 0. Counts are
    rounded to the nearest int (ACS publishes integer estimates; the
    package's nullable Int64 / Float64 typing is preserved through the
    rounding).

    Args:
        row: one row of an ACS DataFrame produced by
            `acs_fetch.fetch_acs_data` (tract or block-group geography —
            same shape).
        config: BracketConfig describing the rollup. The `denominator_var`
            field is not consulted here (it's used by `subtotal_moe_flagged`
            and by callers that want to compare bin sums against the
            authoritative subtotal).

    Returns:
        {bin_id: count} dict, one entry per `config["bin_order"]` id.
        Sum equals the sum of contributing bracket cells (NA → 0).
    """
    out: dict[str, int] = {bid: 0 for bid in config["bin_order"]}
    for code, bin_id in config["bracket_to_bin"].items():
        cell = row.get(code)
        if cell is None or pd.isna(cell):
            continue
        out[bin_id] += int(round(float(cell)))
    return out


def read_authoritative_total(
    row: pd.Series,
    var: str,
) -> Optional[int]:
    """
    Read a single ACS variable as the authoritative population total for
    the tract.

    Recommended for tenure subtotals (B25003_002E for owner, B25003_003E
    for renter) which are less subject to suppression than per-table
    subtotal cells like B25118_014E. Returns None if the cell is
    suppressed.

    Args:
        row: one row of an ACS DataFrame.
        var: variable code to read (e.g. "B25003_003E").

    Returns:
        Integer count, or None if suppressed / missing.
    """
    cell = row.get(var)
    if cell is None or pd.isna(cell):
        return None
    return int(round(float(cell)))


def subtotal_moe_flagged(
    row: pd.Series,
    est_code: str,
    cv_threshold: float = DEFAULT_MOE_CV_THRESHOLD,
) -> bool:
    """
    Return True if the subtotal cell's CV (MOE / estimate) exceeds
    `cv_threshold`.

    The MOE column name is derived by replacing the trailing 'E' with 'M'
    on `est_code` (ACS convention; see `acs_fetch._moe_of`). Cases that
    do NOT flag:
      - estimate is None / NaN / 0 (CV is undefined)
      - MOE is None / NaN
    These match the convention used by Core_BTR's bg_demand._moe_flags
    and elsewhere in the package: when CV is undefined we do not flag,
    rather than treating it as "definitely unreliable".

    Args:
        row: one row of an ACS DataFrame.
        est_code: variable code of the subtotal cell to check
            (e.g. "B25118_014E" for the renter-income subtotal).
        cv_threshold: CV value above which the subtotal is flagged.
            Default 0.40, matching the convention established in
            Core_BTR's bg_demand.MOE_CV_THRESHOLD.

    Returns:
        True if CV > threshold, False otherwise (including the undefined
        cases listed above).
    """
    moe_col = est_code[:-1] + "M"
    est = row.get(est_code)
    moe = row.get(moe_col)
    if (
        est is None or pd.isna(est) or est == 0
        or moe is None or pd.isna(moe)
    ):
        return False
    cv = float(abs(moe)) / float(abs(est))
    return cv > cv_threshold
