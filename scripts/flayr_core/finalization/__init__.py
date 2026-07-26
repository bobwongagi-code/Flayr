"""PR-0A typed views and the compatibility entry around LEGACY_V1.

This package does not own severity rules, candidate qualification, planning,
budgeting, or Phase C execution. Its production entry delegates to LEGACY_V1.
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
    legacy_phase_c_candidate_set,
    legacy_provisional_projection,
    legacy_terminal_projection,
    resolve,
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
    "legacy_phase_c_candidate_set",
    "legacy_provisional_projection",
    "legacy_terminal_projection",
    "resolve",
]
