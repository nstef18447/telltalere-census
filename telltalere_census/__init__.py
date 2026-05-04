"""
telltalere-census — shared Census PUMS / ACS data layer for Telltale RE.

See README.md for module layout and usage.
"""

__version__ = "0.1.0"

from .binning import bin_acs_row
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

__all__ = [
    "BracketConfig",
    "DEFAULT_HIGH_INCOME_SUB_BRACKETS",
    "IpfResult",
    "LifestageGrid",
    "RCLCO_DEFAULT_LIFESTAGE_GRID",
    "TailDecomposition",
    "aggregate_to_lifestages",
    "aggregate_to_lifestages_with_tail",
    "apply_tail_decomposition",
    "apply_tail_decomposition_to_crosstab",
    "bin_acs_row",
    "column_major_to_joint",
    "compute_puma_joint",
    "compute_tract_marginal",
    "decompose_high_income_tail",
    "download_tract_polygons",
    "joint_to_column_major",
    "load_tract_polygons",
    "load_tract_polygons_as_gdf",
    "load_tract_to_puma_crosswalk",
    "proportional_from_brackets",
    "rake_to_marginals",
    "read_authoritative_total",
    "subtotal_moe_flagged",
    "wkb_to_geojson",
]
