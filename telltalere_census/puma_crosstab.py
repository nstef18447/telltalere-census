"""
PUMS-derived joint distributions across arbitrary already-binned dimensions.

`compute_puma_joint` aggregates a PUMS dataframe along one or more
already-binned categorical columns, applying an optional row-level filter
function and weighting by `WGTP` (default; pass `weight_col="PWGTP"` for
person weights). Output is a MultiIndex DataFrame with a single integer
`weight` column, suitable for multi-dimensional rollups by the caller.

The two adapter helpers (`joint_to_column_major`, `column_major_to_joint`)
bridge between this DataFrame shape and the column-major dict-of-dicts
shape that `ipf.rake_to_marginals` consumes / produces. Composition for
the BTR use-case:

    binned = pums_df.assign(
        income_bin=bin_hincp_series(pums_df["HINCP"]),
        age_bin=bin_agep_series(pums_df["AGEP"]),
    )
    joint = compute_puma_joint(
        binned,
        dimensions=["income_bin", "age_bin"],
        filter_func=lambda d: d["TEN"].isin([3, 4]),
    )
    prior = joint_to_column_major(
        joint, row_dim="income_bin", col_dim="age_bin",
        row_order=INCOME_BIN_IDS, col_order=AGE_BIN_IDS,
    )
    raked = rake_to_marginals(prior, row_marginal=..., col_marginal=...)
    raked_df = column_major_to_joint(raked["joint"], "income_bin", "age_bin")

What this module does NOT do — it's all pushed to the caller:
  - Tenure filtering (caller supplies via `filter_func`)
  - Bin assignment (caller pre-bins; vectorized binners are
    consumer-specific and live with the consumer's bracket configuration)
  - Bin-id ordering inside the result MultiIndex (only combinations that
    appear in the data are emitted; the adapter applies the canonical
    order via `row_order` / `col_order`)
"""

from __future__ import annotations

from typing import Callable, Optional

import pandas as pd


def compute_puma_joint(
    pums_records: pd.DataFrame,
    dimensions: list[str],
    weight_col: str = "WGTP",
    filter_func: Optional[Callable[[pd.DataFrame], pd.Series]] = None,
) -> pd.DataFrame:
    """
    Compute a weighted joint distribution from PUMS records along arbitrary
    already-binned dimensions.

    Args:
        pums_records: PUMS dataframe — typically the output of
            `pums_fetch.fetch_pums_data` with derived bin columns added by
            the caller. The function does not bin; the columns named in
            `dimensions` must already contain the categorical bin labels.
        dimensions: column names in `pums_records` to crosstab on. Order
            of this list defines the MultiIndex level order in the result.
        weight_col: PUMS weight column. Default `"WGTP"` (housing-unit
            weights). Use `"PWGTP"` for person weights.
        filter_func: optional row-level filter. Called with the records
            DataFrame and returns a boolean Series; only True rows
            contribute. Use for tenure filters
            (e.g. `lambda d: d["TEN"].isin([3, 4])` for renters) or any
            other slicing the package itself does not encode.

    Returns:
        DataFrame indexed by `dimensions` (MultiIndex when len > 1, plain
        Index when len == 1), with a single `weight` column of integer
        weighted counts. Combinations that contain NaN in any dimension
        or in the weight column are dropped (binners that return NaN for
        unbinnable values are the convention; this is the dual filter).

        Empty index when filter excludes all rows or input is empty.
    """
    if not dimensions:
        raise ValueError("dimensions must contain at least one column name")

    df = pums_records
    if filter_func is not None:
        mask = filter_func(df)
        df = df[mask]

    if df.empty:
        idx = pd.MultiIndex.from_arrays(
            [[] for _ in dimensions], names=dimensions,
        ) if len(dimensions) > 1 else pd.Index([], name=dimensions[0])
        return pd.DataFrame({"weight": pd.Series([], dtype="int64")}, index=idx)

    sub = df[list(dimensions) + [weight_col]].dropna(
        subset=list(dimensions) + [weight_col]
    )
    if sub.empty:
        idx = pd.MultiIndex.from_arrays(
            [[] for _ in dimensions], names=dimensions,
        ) if len(dimensions) > 1 else pd.Index([], name=dimensions[0])
        return pd.DataFrame({"weight": pd.Series([], dtype="int64")}, index=idx)

    grouped = (
        sub.groupby(list(dimensions), observed=True)[weight_col]
        .sum()
        .round()
        .astype("int64")
    )
    return grouped.to_frame(name="weight")


def joint_to_column_major(
    joint: pd.DataFrame,
    row_dim: str,
    col_dim: str,
    row_order: list[str],
    col_order: list[str],
) -> dict[str, dict[str, int]]:
    """
    Convert a 2-D joint DataFrame to the column-major dict-of-dicts shape
    consumed by `ipf.rake_to_marginals`.

    The result has every (col_id, row_id) entry from `col_order` x
    `row_order` populated — combinations absent from the input get 0.
    This matches Core_BTR's `puma_crosstab._weighted_pivot` zero-fill
    convention so consumers can feed the result directly into IPF without
    a separate normalization pass.

    Args:
        joint: DataFrame indexed by (row_dim, col_dim), with a `weight`
            column. Output of `compute_puma_joint(..., dimensions=[row_dim,
            col_dim])`. Index can be in either dimension order; the
            function selects by index level name.
        row_dim, col_dim: dimension names — must match index level names
            in `joint`.
        row_order, col_order: ordered lists of row / col bin ids. The
            result preserves these orderings; ids not present in the
            input are filled with 0.

    Returns:
        `{col_id: {row_id: int_count}}` nested dict. Both nesting levels
        iterate in the order defined by `col_order` / `row_order`.
    """
    if joint.index.nlevels != 2:
        raise ValueError(
            f"joint must have exactly 2 index levels, got {joint.index.nlevels}"
        )
    level_names = list(joint.index.names)
    if row_dim not in level_names or col_dim not in level_names:
        raise ValueError(
            f"row_dim={row_dim!r} or col_dim={col_dim!r} not in index level "
            f"names {level_names}"
        )

    out: dict[str, dict[str, int]] = {
        col_id: {row_id: 0 for row_id in row_order} for col_id in col_order
    }
    for idx, w in joint["weight"].items():
        # idx is a tuple ordered as joint.index.names; map by name.
        as_named = dict(zip(level_names, idx))
        col_key = str(as_named[col_dim])
        row_key = str(as_named[row_dim])
        if col_key in out and row_key in out[col_key]:
            out[col_key][row_key] = int(w)
    return out


def column_major_to_joint(
    column_major: dict[str, dict[str, int]],
    row_dim_name: str,
    col_dim_name: str,
) -> pd.DataFrame:
    """
    Inverse of `joint_to_column_major`: convert a column-major dict
    (typically the `joint` field of an `IpfResult`) back to the MultiIndex
    DataFrame shape produced by `compute_puma_joint`.

    Args:
        column_major: `{col_id: {row_id: int_count}}` nested dict.
        row_dim_name: name to assign to the row level of the result's
            MultiIndex.
        col_dim_name: name to assign to the column level.

    Returns:
        DataFrame with MultiIndex levels (row_dim_name, col_dim_name) and
        a single `weight` column (int64). Iteration order follows the
        input dict's key order — outer (column) first, inner (row) within.
    """
    rows: list[tuple[str, str, int]] = []
    for col_id, row_dict in column_major.items():
        for row_id, w in row_dict.items():
            rows.append((str(row_id), str(col_id), int(w)))
    if not rows:
        idx = pd.MultiIndex.from_arrays(
            [[], []], names=[row_dim_name, col_dim_name],
        )
        return pd.DataFrame({"weight": pd.Series([], dtype="int64")}, index=idx)
    df = pd.DataFrame(rows, columns=[row_dim_name, col_dim_name, "weight"])
    return df.set_index([row_dim_name, col_dim_name])
