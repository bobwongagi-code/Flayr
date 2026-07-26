"""PR-0A typed views around the existing LEGACY_V1 finalization path.

This package is intentionally projection-only. It does not own severity
rules, candidate qualification, planning, budgeting, or Phase C execution.
"""

from .contracts import (
    LegacyConstraintView,
    LegacyPhaseCCandidateSet,
    LegacyPhaseCCandidateView,
    LegacyProvisionalProjection,
    LegacySeverityResolutionProjection,
    LegacyTerminalProjection,
    SeverityResolutionFacade,
)
from .facade import (
    from_legacy_resolution,
    legacy_provisional_projection,
    legacy_terminal_projection,
)

__all__ = [
    "LegacyConstraintView",
    "LegacyPhaseCCandidateSet",
    "LegacyPhaseCCandidateView",
    "LegacyProvisionalProjection",
    "LegacySeverityResolutionProjection",
    "LegacyTerminalProjection",
    "SeverityResolutionFacade",
    "from_legacy_resolution",
    "legacy_provisional_projection",
    "legacy_terminal_projection",
]
