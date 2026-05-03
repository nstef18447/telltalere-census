"""
Iterative Proportional Fitting (matrix raking) for 2-D joint distributions.

`rake_to_marginals` takes a 2-D seed joint (the prior — typically a PUMA-
level crosstab from PUMS) and rakes it to a pair of marginal distributions
(the targets — typically tract-level row/column totals from ACS bracket
rollups), producing a synthesized joint at the marginal-source geography.

The algorithm is the standard biproportional row/column scaling:
  1. Normalize the prior to a probability distribution (sum = 1).
  2. Scale to the target total (= average of row / col marginal sums when
     they disagree, which they will slightly because they come from
     independent ACS tables).
  3. Iterate: scale rows to match `row_marginal`, then scale columns to
     match `col_marginal`. Stop when the largest cell delta between
     iterations is < `tolerance` or `max_iterations` is hit.
  4. Round to integers. Two-pass reconcile-to-marginals afterward:
     (a) fix grand-total drift via cell-level redistribute (the +/- 1s
         from np.rint that don't cancel out)
     (b) fix per-row and per-col drift via unit shifts that preserve the
         grand total and, where possible, the other axis's sums.

Edge cases (handled explicitly, returned in metadata):
  - Marginal value = 0 → corresponding row/column is forced to all zeros
    before iteration (avoids division by zero scaling).
  - Prior cell = 0 with nonzero marginals → cell stays 0. The PUMA prior
    is asserting "this combination doesn't occur in the population", and
    the IPF respects that.
  - Prior is all zeros for a row that has nonzero marginal → fall back to
    a uniform row (split evenly across the columns with positive col
    marginals). Degenerate input — uniform is the most neutral imputation.
    Flagged in metadata as `fallback_rows_uniform`.

Scope: 2-D only. N-D rake is a different algorithm; not in current scope.

Behavioral parity: this is a verbatim port of Core_BTR's
backend.ipf_crosstab.ipf_2d. The algorithm body, defaults, and rounding
behavior are preserved exactly so consumers can swap one for the other
and produce byte-identical output.
"""

from __future__ import annotations

from typing import TypedDict

import numpy as np


_IPF_TOLERANCE_DEFAULT = 0.5      # cell delta in count units; 0.5 → cells settle to within half a unit
_IPF_MAX_ITERATIONS_DEFAULT = 50
_RESIDUAL_TOLERANCE_PCT = 0.01    # max acceptable rounding drift before redistribution


class IpfResult(TypedDict):
    """
    Result of an IPF synthesis.

    Attributes:
        joint: column-major nested dict — `joint[col_id][row_id] = count`.
            Same shape as the input `prior` argument.
        converged: True when the algorithm met `tolerance` inside
            `max_iterations`. False otherwise; the result is still returned
            (inspect `iterations` and `final_max_delta` to judge usability).
        iterations: actual number of iterations run.
        final_max_delta: max-cell-level absolute delta between the last
            two iterations, in count units.
        rounding_residual: signed difference between the integer target
            total and the sum of the rounded result. Should be 0 in normal
            operation; a nonzero value indicates the post-rounding
            reconciliation could not fully close the drift.
        fallback_rows_uniform: list of row ids where the prior had zero
            mass but the marginal demanded nonzero counts; uniform across
            active columns was used as the imputation.
        zero_marginal_rows: row ids whose row-marginal value was 0.
        zero_marginal_cols: col ids whose col-marginal value was 0.
    """

    joint: dict[str, dict[str, int]]
    converged: bool
    iterations: int
    final_max_delta: float
    rounding_residual: int
    fallback_rows_uniform: list[str]
    zero_marginal_rows: list[str]
    zero_marginal_cols: list[str]


def _to_matrix(
    prior: dict[str, dict[str, float]],
    row_ids: list[str],
    col_ids: list[str],
) -> np.ndarray:
    """Convert a column-major dict to a 2D numpy array shaped (rows, cols)."""
    m = np.zeros((len(row_ids), len(col_ids)), dtype=np.float64)
    for c, col_id in enumerate(col_ids):
        col_dict = prior.get(col_id, {})
        for r, row_id in enumerate(row_ids):
            v = col_dict.get(row_id, 0.0)
            m[r, c] = float(v) if v is not None else 0.0
    return m


def _from_matrix(
    matrix: np.ndarray,
    row_ids: list[str],
    col_ids: list[str],
) -> dict[str, dict[str, int]]:
    """Convert a 2D numpy int array back to column-major dict."""
    out: dict[str, dict[str, int]] = {}
    for c, col_id in enumerate(col_ids):
        out[col_id] = {row_id: int(matrix[r, c]) for r, row_id in enumerate(row_ids)}
    return out


def _reconcile_to_marginals(
    M: np.ndarray,
    row_target: np.ndarray,
    col_target: np.ndarray,
    max_passes: int = 500,
) -> np.ndarray:
    """
    Greedy unit-shift reconciliation. Adjusts cells by +/-1 to drive
    per-row and per-col sums to their targets while preserving the grand
    total (caller is responsible for the grand-total fix before invoking
    this).

    Four cases per pass, taken in order:
      (1) row over + col over: decrement an over-row x over-col cell.
      (2) row under + col under: increment an under-row x under-col cell.
      (3) row drift balanced, col drift unbalanced: swap a unit from an
          over-col to an under-col within some row (preserves row sums).
      (4) col drift balanced, row drift unbalanced: swap a unit between
          rows within some col (preserves col sums).

    Each pass strictly reduces |row_drift| + |col_drift| or terminates.
    Cells are never pushed below zero. Bail-out (no valid move) returns
    the matrix as-is — caller treats remaining drift as failure to
    reconcile and reports it via `rounding_residual` / row-col diff.
    """
    M = M.copy()
    n_rows, n_cols = M.shape

    for _ in range(max_passes):
        rd = M.sum(axis=1) - row_target
        cd = M.sum(axis=0) - col_target

        if not np.any(rd) and not np.any(cd):
            return M

        # Case 1: over-row + over-col → decrement
        over_r = np.where(rd > 0)[0]
        over_c = np.where(cd > 0)[0]
        moved = False
        for i in over_r:
            for j in over_c:
                if M[i, j] > 0:
                    M[i, j] -= 1
                    moved = True
                    break
            if moved:
                break
        if moved:
            continue

        # Case 2: under-row + under-col → increment
        under_r = np.where(rd < 0)[0]
        under_c = np.where(cd < 0)[0]
        if len(under_r) > 0 and len(under_c) > 0:
            i = int(under_r[0])
            j = int(under_c[0])
            M[i, j] += 1
            continue

        # Case 3: row drift balanced, col drift unbalanced
        if not np.any(rd) and np.any(cd):
            j_over_arr = np.where(cd > 0)[0]
            j_under_arr = np.where(cd < 0)[0]
            if len(j_over_arr) > 0 and len(j_under_arr) > 0:
                j_over = int(j_over_arr[0])
                j_under = int(j_under_arr[0])
                rows_with_mass = np.where(M[:, j_over] > 0)[0]
                if len(rows_with_mass) > 0:
                    i = int(rows_with_mass[0])
                    M[i, j_over] -= 1
                    M[i, j_under] += 1
                    continue

        # Case 4: col drift balanced, row drift unbalanced
        if not np.any(cd) and np.any(rd):
            i_over_arr = np.where(rd > 0)[0]
            i_under_arr = np.where(rd < 0)[0]
            if len(i_over_arr) > 0 and len(i_under_arr) > 0:
                i_over = int(i_over_arr[0])
                i_under = int(i_under_arr[0])
                cols_with_mass = np.where(M[i_over, :] > 0)[0]
                if len(cols_with_mass) > 0:
                    j = int(cols_with_mass[0])
                    M[i_over, j] -= 1
                    M[i_under, j] += 1
                    continue

        # No valid move found — bail out
        return M

    return M


def rake_to_marginals(
    prior: dict[str, dict[str, float]],
    row_marginal: dict[str, int],
    col_marginal: dict[str, int],
    max_iterations: int = _IPF_MAX_ITERATIONS_DEFAULT,
    tolerance: float = _IPF_TOLERANCE_DEFAULT,
) -> IpfResult:
    """
    Rake a 2-D prior to row/column marginals via iterative proportional fitting.

    Args:
        prior: column-major joint dict — `prior[col_id][row_id]` is the
            unweighted-but-relative joint count from the seed source.
            Typically derived from PUMS at PUMA level via
            `puma_crosstab.compute_puma_joint` + `joint_to_column_major`.
        row_marginal: target row totals keyed by row id (e.g. income bins).
        col_marginal: target column totals keyed by col id (e.g. age bins
            or HH-size bins).
        max_iterations: stopping cap on iteration count.
        tolerance: max-cell-delta tolerance for convergence (in count units).

    Returns:
        IpfResult — see TypedDict definition.

    Notes:
        - Row and col ids are inferred from `row_marginal` / `col_marginal`
          key order. Callers should construct the marginal dicts using a
          canonical bin id ordering so the result is in a stable shape.
        - Row/col marginals come from independent ACS tables and may
          disagree on the grand total (small drift expected). The function
          uses the average of the two sums to scale the prior to the
          target total; per-axis differences are absorbed by the iteration
          and post-rounding reconciliation.
    """
    row_ids = list(row_marginal.keys())
    col_ids = list(col_marginal.keys())
    n_rows = len(row_ids)
    n_cols = len(col_ids)

    row_target = np.array([float(row_marginal[r]) for r in row_ids], dtype=np.float64)
    col_target = np.array([float(col_marginal[c]) for c in col_ids], dtype=np.float64)

    zero_marginal_rows = [row_ids[i] for i in range(n_rows) if row_target[i] == 0.0]
    zero_marginal_cols = [col_ids[j] for j in range(n_cols) if col_target[j] == 0.0]

    row_sum = float(row_target.sum())
    col_sum = float(col_target.sum())
    target_total = (row_sum + col_sum) / 2.0 if row_sum > 0 and col_sum > 0 else max(row_sum, col_sum)

    if target_total <= 0:
        empty = {col_id: {row_id: 0 for row_id in row_ids} for col_id in col_ids}
        return {
            "joint": empty,
            "converged": True,
            "iterations": 0,
            "final_max_delta": 0.0,
            "rounding_residual": 0,
            "fallback_rows_uniform": [],
            "zero_marginal_rows": zero_marginal_rows,
            "zero_marginal_cols": zero_marginal_cols,
        }

    matrix = _to_matrix(prior, row_ids, col_ids)

    # Zero out rows/cols whose marginal is zero — they shouldn't carry mass.
    for i in range(n_rows):
        if row_target[i] == 0.0:
            matrix[i, :] = 0.0
    for j in range(n_cols):
        if col_target[j] == 0.0:
            matrix[:, j] = 0.0

    # Detect rows where the prior has zero mass but the marginal is
    # nonzero. Fall back to uniform across columns with nonzero col
    # marginal — the prior had no observations in this combination, so
    # any imputation is uncertain; uniform is the most neutral.
    fallback_rows_uniform: list[str] = []
    for i in range(n_rows):
        if row_target[i] > 0 and matrix[i, :].sum() == 0.0:
            nonzero_cols = col_target > 0
            n_active = int(nonzero_cols.sum())
            if n_active > 0:
                matrix[i, nonzero_cols] = 1.0 / n_active
                fallback_rows_uniform.append(row_ids[i])

    # Normalize prior to probability and scale to target total — IPF
    # iterations are scale-equivariant, so this just makes the early
    # iterations land closer to the answer.
    total_mass = matrix.sum()
    if total_mass > 0:
        matrix = matrix * (target_total / total_mass)

    converged = False
    iterations = 0
    final_max_delta = float("inf")

    for it in range(max_iterations):
        iterations = it + 1
        prev = matrix.copy()

        row_sums = matrix.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            row_scale = np.where(row_sums > 0, row_target / row_sums, 0.0)
        matrix = matrix * row_scale[:, np.newaxis]

        col_sums = matrix.sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            col_scale = np.where(col_sums > 0, col_target / col_sums, 0.0)
        matrix = matrix * col_scale[np.newaxis, :]

        delta = float(np.abs(matrix - prev).max())
        final_max_delta = delta
        if delta < tolerance:
            converged = True
            break

    # Round to integers. Two-step reconciliation:
    # (1) fix grand-total drift via cell-level redistribute (the +/- 1s
    #     from np.rint that don't cancel out)
    # (2) fix per-row and per-col drift via unit shifts that preserve
    #     the grand total. Both passes are deterministic.
    rounded = np.rint(matrix).astype(np.int64)
    target_int = int(round(target_total))
    initial_residual = target_int - int(rounded.sum())

    if initial_residual != 0 and rounded.size > 0:
        drift_pct = abs(initial_residual) / max(target_int, 1)
        if drift_pct <= _RESIDUAL_TOLERANCE_PCT:
            flat = rounded.flatten()
            order = np.argsort(-flat, kind="stable")
            step = 1 if initial_residual > 0 else -1
            i = 0
            remaining = abs(initial_residual)
            while remaining > 0 and i < flat.size:
                idx = order[i]
                if step < 0 and flat[idx] <= 0:
                    i += 1
                    continue
                flat[idx] += step
                remaining -= 1
                i += 1
            rounded = flat.reshape(rounded.shape)
        # else: drift > 1%, leave it — caller inspects rounding_residual

    rounded = _reconcile_to_marginals(
        rounded,
        np.array([int(row_target[i]) for i in range(n_rows)], dtype=np.int64),
        np.array([int(col_target[j]) for j in range(n_cols)], dtype=np.int64),
    )

    final_residual = target_int - int(rounded.sum())

    return {
        "joint": _from_matrix(rounded, row_ids, col_ids),
        "converged": converged,
        "iterations": iterations,
        "final_max_delta": final_max_delta,
        "rounding_residual": final_residual,
        "fallback_rows_uniform": fallback_rows_uniform,
        "zero_marginal_rows": zero_marginal_rows,
        "zero_marginal_cols": zero_marginal_cols,
    }
