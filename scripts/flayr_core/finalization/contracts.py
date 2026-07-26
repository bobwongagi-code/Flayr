"""Compatibility-only contracts for ADR-006 PR-0A.

The values in this module are the repository's existing string values. These
dataclasses are read-only views and do not replace the domain model or the
legacy JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias


LegacyPhaseCPolicyVersion: TypeAlias = Literal["legacy-v1"]


@dataclass(frozen=True, kw_only=True)
class LegacyConstraintView:
    """Immutable view of one existing legacy severity constraint."""

    kind: str
    level: str
    rule: str
    reason: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SeverityResolutionFacade:
    """Typed view of the existing resolver result without candidate state."""

    model_severity: str
    resolved_severity: str
    floor: str | None
    ceiling: str | None
    status: str
    constraints: tuple[LegacyConstraintView, ...] = ()


@dataclass(frozen=True, kw_only=True)
class LegacySeverityResolutionProjection:
    """Lossless legacy resolver projection, including its legacy flag."""

    resolution: SeverityResolutionFacade
    phase_c_candidate: bool


@dataclass(frozen=True, kw_only=True)
class LegacyPhaseCCandidateView:
    """The existing LEGACY_V1 stage candidate, without V2 reason semantics."""

    stage_id: str
    legacy_source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class LegacyPhaseCCandidateSet:
    """Legacy candidate collection used only by the compatibility path."""

    candidates: tuple[LegacyPhaseCCandidateView, ...] = ()
    policy_version: LegacyPhaseCPolicyVersion = field(
        init=False,
        default="legacy-v1",
    )


@dataclass(frozen=True, kw_only=True)
class LegacyProvisionalProjection:
    """Compatibility view for the pre-Phase-C result boundary."""

    severity_resolutions: tuple[SeverityResolutionFacade, ...] = ()
    candidate_set: LegacyPhaseCCandidateSet = field(
        default_factory=LegacyPhaseCCandidateSet,
    )


@dataclass(frozen=True, kw_only=True)
class LegacyTerminalProjection:
    """Compatibility view for the terminal boundary; it has no candidates."""

    severity_resolutions: tuple[SeverityResolutionFacade, ...] = ()
