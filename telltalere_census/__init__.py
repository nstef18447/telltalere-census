"""
telltalere-census — shared Census PUMS / ACS data layer for Telltale RE.

See README.md for module layout and usage.
"""

__version__ = "0.1.0"

from .binning import bin_acs_row
from .boundaries import (
    download_tract_boundary_crosswalk,
    get_boundary_vintage_for_acs,
    harmonize_to_2020_boundaries,
    load_tract_boundary_crosswalk,
)
from .bracket_allocation import proportional_from_brackets
from .geometry import (
    download_tract_polygons,
    load_tract_polygons,
    load_tract_polygons_as_gdf,
    load_tract_to_puma_crosswalk,
    wkb_to_geojson,
)
from .income_tail import (
    DEFAULT_HIGH_INCOME_SUB_BRACKETS,
    TailDecomposition,
    apply_tail_decomposition,
    apply_tail_decomposition_to_crosstab,
    decompose_high_income_tail,
)
from .ipf import IpfResult, rake_to_marginals
from .lifestage import (
    RCLCO_DEFAULT_LIFESTAGE_GRID,
    LifestageGrid,
    aggregate_to_lifestages,
    aggregate_to_lifestages_with_tail,
)
from .multi_vintage import (
    fetch_acs_data_multi_vintage,
    get_available_vintages,
)
from .puma_crosstab import (
    column_major_to_joint,
    compute_puma_joint,
    joint_to_column_major,
)
from .tract_marginals import (
    BracketConfig,
    compute_tract_marginal,
    read_authoritative_total,
    subtotal_moe_flagged,
)
from .trends import (
    TrendResult,
    cagr,
    compute_trend_metrics,
    cumulative_trend,
    is_change_significant,
)

__all__ = [
    "BracketConfig",
    "DEFAULT_HIGH_INCOME_SUB_BRACKETS",
    "IpfResult",
    "LifestageGrid",
    "RCLCO_DEFAULT_LIFESTAGE_GRID",
    "TailDecomposition",
    "TrendResult",
    "aggregate_to_lifestages",
    "aggregate_to_lifestages_with_tail",
    "apply_tail_decomposition",
    "apply_tail_decomposition_to_crosstab",
    "bin_acs_row",
    "cagr",
    "column_major_to_joint",
    "compute_puma_joint",
    "compute_tract_marginal",
    "compute_trend_metrics",
    "cumulative_trend",
    "decompose_high_income_tail",
    "download_tract_boundary_crosswalk",
    "download_tract_polygons",
    "fetch_acs_data_multi_vintage",
    "get_available_vintages",
    "get_boundary_vintage_for_acs",
    "harmonize_to_2020_boundaries",
    "is_change_significant",
    "joint_to_column_major",
    "load_tract_boundary_crosswalk",
    "load_tract_polygons",
    "load_tract_polygons_as_gdf",
    "load_tract_to_puma_crosswalk",
    "proportional_from_brackets",
    "rake_to_marginals",
    "read_authoritative_total",
    "subtotal_moe_flagged",
    "wkb_to_geojson",
]
