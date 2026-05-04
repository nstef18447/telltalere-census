"""
Pure-function trend computation primitives for ACS multi-vintage data.

Operates on `dict[int, float]` inputs (vintage end-year -> value). Stateless,
no I/O, no Census API dependency. Pair with `multi_vintage.fetch_acs_data_multi_vintage`
to produce the input dicts.

Inflation-comparability caveat
------------------------------
Trend metrics computed on income-bracket variables (B25118, B19001, etc.)
are over **vintage-native nominal dollars**. A "$100,000+" bracket in
2014-2018 represents different real purchasing power than the same
nominal label in 2019-2023. Cross-vintage comparison of nominal income
brackets is mathematically valid but economically misleading without a
CPI deflator. CPI adjustment is not provided by this package; consumers
can apply deflators at their own layer. See `multi_vintage` module
docstring for the full discussion.

The same caveat applies to median rent (B25064), median home value
(B25077), and any other dollar-denominated variable. Population counts,
tenure counts, and bracket *shares* (computed as a percent of the row
total) are not affected.

MOE significance testing
------------------------
ACS estimates carry margins of error. A "10% increase in median rent"
that is smaller than the combined MOE of the two endpoints is not
statistically distinguishable from noise. `is_change_significant` and
the `is_significant` flag on `compute_trend_metrics` return False (and
the direction classifies as "noise" rather than "flat") when MOEs
overlap.
"""

from __future__ import annotations

from typing import Literal, Optional, TypedDict

import math


_DirectionFlag = Literal["increasing", "decreasing", "flat", "noise"]
_SignificanceMethod = Literal["moe_combined", "moe_overlap"]


class TrendResult(TypedDict):
    """Result of computing trend metrics for a value over multiple vintages.

    Field semantics
    ---------------
    `early_value`, `late_value`: the values from the earliest and latest
        vintages in the input. Not the min/max — this is a temporal
        comparison, not an extremum.
    `early_vintage`, `late_vintage`: the vintage end-years corresponding
        to those values.
    `years_elapsed`: late_vintage - early_vintage. Always positive (the
        function sorts ascending before computing).
    `absolute_change`: late - early. None if either endpoint is null/NaN.
    `percent_change`: (late - early) / early. None if early is 0 or null
        (percent change is undefined for a zero base).
    `cagr`: compound annual growth rate, ((late/early) ** (1/years)) - 1.
        None if early <= 0, late < 0, or years <= 0.
    `direction`: one of "increasing", "decreasing", "flat", "noise". See
        the function-level docstring for classification rules.
    `is_significant`: MOE-aware change significance. False if MOEs not
        provided or if the change is smaller than the combined MOE.
    `moe_combined`: sqrt(early_moe^2 + late_moe^2) if MOEs provided, else
        None.
    """

    early_value: float
    late_value: float
    early_vintage: int
    late_vintage: int
    years_elapsed: int
    absolute_change: Optional[float]
    percent_change: Optional[float]
    cagr: Optional[float]
    direction: _DirectionFlag
    is_significant: bool
    moe_combined: Optional[float]


# ---------------------------------------------------------------------------
# Atomic primitives
# ---------------------------------------------------------------------------

def cagr(
    early_value: float,
    late_value: float,
    years: int,
) -> Optional[float]:
    """Compound annual growth rate: ((late / early) ** (1 / years)) - 1.

    Returns None when CAGR is mathematically undefined or unstable:
    - Either value is null/NaN.
    - early_value is non-positive (a non-positive base makes the ratio
      undefined or yields the wrong sign convention).
    - late_value is negative (cannot take a real-valued root of a
      negative number for fractional powers).
    - years <= 0 (CAGR over zero or negative time has no meaning).

    Note: late_value == 0 is allowed (returns -1.0, "100% loss per year"
    is a valid CAGR result). early_value == 0 is not (division by zero).
    """
    if _is_null(early_value) or _is_null(late_value):
        return None
    if early_value <= 0:
        return None
    if late_value < 0:
        return None
    if years <= 0:
        return None
    return (late_value / early_value) ** (1 / years) - 1


def is_change_significant(
    early_value: float,
    early_moe: float,
    late_value: float,
    late_moe: float,
    method: _SignificanceMethod = "moe_combined",
) -> bool:
    """Test whether a change between two ACS estimates is statistically
    distinguishable given their margins of error.

    `moe_combined` (Census Bureau's recommended method for testing the
    significance of a difference between two ACS estimates):
        |late - early| > sqrt(early_moe**2 + late_moe**2)

    `moe_overlap` (geometric: do the 90% confidence intervals overlap?):
        max(early - early_moe, late - late_moe) > min(early + early_moe, late + late_moe)

    The combined-MOE test is strictly tighter than the overlap test for
    the typical case (both MOEs positive); some changes will register as
    significant under combined-MOE but not under overlap, even though the
    overlap test is what most readers visualize. The default reflects
    Census's published guidance.

    Returns False if any input is null/NaN.
    """
    if _is_null(early_value) or _is_null(early_moe):
        return False
    if _is_null(late_value) or _is_null(late_moe):
        return False
    diff = abs(late_value - early_value)
    if method == "moe_combined":
        combined = math.sqrt(early_moe ** 2 + late_moe ** 2)
        return diff > combined
    if method == "moe_overlap":
        early_low, early_high = early_value - early_moe, early_value + early_moe
        late_low, late_high = late_value - late_moe, late_value + late_moe
        # CIs overlap iff max(lows) <= min(highs); change is significant
        # iff CIs do NOT overlap.
        return max(early_low, late_low) > min(early_high, late_high)
    raise ValueError(f"unknown significance method {method!r}")


# ---------------------------------------------------------------------------
# Aggregate primitives
# ---------------------------------------------------------------------------

def compute_trend_metrics(
    values_by_vintage: dict[int, float],
    moes_by_vintage: Optional[dict[int, float]] = None,
    direction_threshold: float = 0.05,
    significance_method: _SignificanceMethod = "moe_combined",
) -> TrendResult:
    """Compute trend metrics from earliest to latest vintage in the input.

    Uses only the earliest and latest vintages; intermediate vintages are
    ignored. For consecutive-pair time-series analysis use `cumulative_trend`.

    Direction classification:
        - "increasing" if percent_change > +direction_threshold
        - "decreasing" if percent_change < -direction_threshold
        - "flat"       if |percent_change| <= direction_threshold AND is_significant
        - "noise"      if |percent_change| <= direction_threshold AND NOT is_significant

    Edge cases:
        - early_value <= 0 forces percent_change to None and CAGR to None.
          Direction defaults to "flat" if is_significant else "noise" (we
          have no reliable percent to threshold against, so we trust the
          MOE signal).
        - early_value == late_value == 0 returns absolute_change=0,
          percent_change=None, direction="flat" or "noise" by MOE.
        - is_significant is False when MOEs not provided.

    Raises:
        ValueError: if values_by_vintage has fewer than 2 entries, or if
            the earliest or latest value is null/NaN.
    """
    if len(values_by_vintage) < 2:
        raise ValueError(
            f"compute_trend_metrics requires >=2 vintages, got {len(values_by_vintage)}"
        )

    sorted_years = sorted(values_by_vintage.keys())
    early_year, late_year = sorted_years[0], sorted_years[-1]
    early_val = values_by_vintage[early_year]
    late_val = values_by_vintage[late_year]

    if _is_null(early_val) or _is_null(late_val):
        raise ValueError(
            f"earliest ({early_year}) or latest ({late_year}) value is null/NaN; "
            "cannot compute trend"
        )

    years_elapsed = late_year - early_year
    abs_change = late_val - early_val
    if early_val == 0:
        pct_change: Optional[float] = None
    else:
        pct_change = abs_change / early_val

    cagr_value = cagr(early_val, late_val, years_elapsed)

    early_moe = moes_by_vintage.get(early_year) if moes_by_vintage else None
    late_moe = moes_by_vintage.get(late_year) if moes_by_vintage else None
    if (
        moes_by_vintage is not None
        and not _is_null(early_moe)
        and not _is_null(late_moe)
    ):
        moe_combined: Optional[float] = math.sqrt(
            float(early_moe) ** 2 + float(late_moe) ** 2
        )
        is_sig = is_change_significant(
            early_val, float(early_moe), late_val, float(late_moe),
            method=significance_method,
        )
    else:
        moe_combined = None
        is_sig = False

    direction = _classify_direction(pct_change, is_sig, direction_threshold)

    return TrendResult(
        early_value=float(early_val),
        late_value=float(late_val),
        early_vintage=int(early_year),
        late_vintage=int(late_year),
        years_elapsed=int(years_elapsed),
        absolute_change=float(abs_change),
        percent_change=pct_change,
        cagr=cagr_value,
        direction=direction,
        is_significant=is_sig,
        moe_combined=moe_combined,
    )


def cumulative_trend(
    values_by_vintage: dict[int, float],
    moes_by_vintage: Optional[dict[int, float]] = None,
    direction_threshold: float = 0.05,
    significance_method: _SignificanceMethod = "moe_combined",
) -> list[TrendResult]:
    """For each consecutive vintage pair (sorted ascending), compute trend
    metrics. Useful for annual 1-year ACS time series.

    Returns a list with len(values_by_vintage) - 1 entries. Empty input
    or a 1-element input raises ValueError (consistent with
    compute_trend_metrics).

    Each pair-result references only that pair's two vintages. To compute
    end-to-end metrics across the full range, call compute_trend_metrics
    on the full dict.
    """
    if len(values_by_vintage) < 2:
        raise ValueError(
            f"cumulative_trend requires >=2 vintages, got {len(values_by_vintage)}"
        )

    sorted_years = sorted(values_by_vintage.keys())
    out: list[TrendResult] = []
    for early_year, late_year in zip(sorted_years[:-1], sorted_years[1:]):
        pair_values = {
            early_year: values_by_vintage[early_year],
            late_year: values_by_vintage[late_year],
        }
        if moes_by_vintage is not None:
            pair_moes: Optional[dict[int, float]] = {
                early_year: moes_by_vintage.get(early_year, float("nan")),
                late_year: moes_by_vintage.get(late_year, float("nan")),
            }
        else:
            pair_moes = None
        out.append(
            compute_trend_metrics(
                pair_values, pair_moes, direction_threshold, significance_method,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _is_null(x: object) -> bool:
    """True if x is None, NaN, or pandas NA."""
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        # pd.NA et al. — treat as null
        return True


def _classify_direction(
    pct_change: Optional[float],
    is_significant: bool,
    threshold: float,
) -> _DirectionFlag:
    """Direction flag rules (see compute_trend_metrics docstring).

    pct_change is None (early was 0 or null) -> trust MOE signal:
        flat if significant else noise.
    """
    if pct_change is None:
        return "flat" if is_significant else "noise"
    if pct_change > threshold:
        return "increasing"
    if pct_change < -threshold:
        return "decreasing"
    return "flat" if is_significant else "noise"
