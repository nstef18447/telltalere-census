"""
Generic bracket-bin mapping for ACS summary-table rows.

`bin_acs_row` collapses a row of ACS bracket estimate cells into a smaller
set of display bins by summing the cells that map to each bin. Used by
consumers (Core_BTR, the demographics inspector, future products) that
maintain their own bracket-to-bin dictionaries — this module supplies the
mechanism only, never the configuration.

Suppressed cells (NaN / None) are treated as 0 — they do not contribute to
any bin and do not poison the totals. This matches the convention used
elsewhere in the package's bracket-handling code (see
`bracket_allocation.proportional_from_brackets`).

Counts are returned as Python ints (rounded from the underlying ACS
estimate, which the API publishes as whole numbers but the package types
as nullable Int64 / Float64).
"""

from __future__ import annotations

import pandas as pd


def bin_acs_row(
    row: pd.Series,
    code_to_bin: dict[str, str],
    bin_ids: list[str],
) -> dict[str, int]:
    """
    Sum ACS bracket-cell estimates onto a smaller set of display bins.

    Args:
        row: one row from an ACS DataFrame (e.g. tract or block-group level
            output of `acs_fetch.fetch_acs_data`). Bracket estimates are
            looked up by column name via `row.get(code)`.
        code_to_bin: mapping from ACS variable code (e.g. "B25118_015E") to
            the display bin id it contributes to. Multiple codes may target
            the same bin — their counts are summed.
        bin_ids: ordered list of all display bin ids. The result dict has
            one entry per id, including bins that receive zero contribution
            (so the output shape is stable across rows).

    Returns:
        Dict from bin id to integer count. Sum equals the sum of all
        `code_to_bin` cells in the row (treating NA as 0).
    """
    out: dict[str, int] = {bid: 0 for bid in bin_ids}
    for code, bin_id in code_to_bin.items():
        cell = row.get(code)
        if cell is None or pd.isna(cell):
            continue
        out[bin_id] += int(round(float(cell)))
    return out
