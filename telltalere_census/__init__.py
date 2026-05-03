"""
telltalere-census — shared Census PUMS / ACS data layer for Telltale RE.

See README.md for module layout and usage.
"""

__version__ = "0.1.0"

from .binning import bin_acs_row
from .bracket_allocation import proportional_from_brackets
from .ipf import IpfResult, rake_to_marginals
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
    "IpfResult",
    "bin_acs_row",
    "column_major_to_joint",
    "compute_puma_joint",
    "compute_tract_marginal",
    "joint_to_column_major",
    "proportional_from_brackets",
    "rake_to_marginals",
    "read_authoritative_total",
    "subtotal_moe_flagged",
]
