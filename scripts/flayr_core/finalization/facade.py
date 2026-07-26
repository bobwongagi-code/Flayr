"""One-way adapters from the existing LEGACY_V1 result structures.

No function in this module changes a legacy result. The adapter copies the
fields needed by the typed views and can reconstruct the resolver's existing
dictionary for equivalence tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .contracts import (
    LegacyConstraintView,
    LegacyPhaseCCandidateSet,
    LegacyPhaseCCandidateView,
    LegacyProvisionalProjection,
    LegacySeverityResolutionProjection,
    LegacyTerminalProjection,
    SeverityResolutionFacade,
)


_STAGE_CODE_RE = re.compile(r"^(S[1-6])(?:\b|$)")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"legacy resolution field {field_name!r} must be a string")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"legacy resolution field {field_name!r} must be a string or None")
    return value


def _constraint_view(value: Any) -> LegacyConstraintView:
    if isinstance(value, Mapping):
        read = value.get
    else:
        read = lambda key, default=None: getattr(value, key, default)

    raw_evidence_ids = read("evidence_ids", ())
    if raw_evidence_ids is None:
        raw_evidence_ids = ()
    if isinstance(raw_evidence_ids, (str, bytes)):
        raise TypeError("legacy constraint evidence_ids must be a sequence")
    if any(not isinstance(item, str) for item in raw_evidence_ids):
        raise TypeError("legacy constraint evidence_ids must contain strings")

    return LegacyConstraintView(
        kind=_required_text(read("kind"), "kind"),
        level=_required_text(read("level"), "level"),
        rule=_required_text(read("rule"), "rule"),
        reason=_required_text(read("reason"), "reason"),
        evidence_ids=tuple(raw_evidence_ids),
    )


def from_legacy_resolution(
    legacy: Mapping[str, Any],
) -> LegacySeverityResolutionProjection:
    """Read one existing ``resolve_severity`` result into immutable views."""

    if not isinstance(legacy, Mapping):
        raise TypeError("legacy resolution must be a mapping")
    required_fields = {
        "model_severity",
        "severity",
        "floor",
        "ceiling",
        "status",
        "constraints",
        "phase_c_candidate",
    }
    missing_fields = sorted(required_fields - set(legacy))
    if missing_fields:
        raise TypeError("legacy resolution is missing fields: " + ", ".join(missing_fields))

    raw_constraints = legacy["constraints"]
    if not isinstance(raw_constraints, (tuple, list)):
        raise TypeError("legacy resolution constraints must be a sequence")
    phase_c_candidate = legacy.get("phase_c_candidate")
    if not isinstance(phase_c_candidate, bool):
        raise TypeError("legacy resolution phase_c_candidate must be a bool")

    resolution = SeverityResolutionFacade(
        model_severity=_required_text(legacy.get("model_severity"), "model_severity"),
        resolved_severity=_required_text(legacy.get("severity"), "severity"),
        floor=_optional_text(legacy.get("floor"), "floor"),
        ceiling=_optional_text(legacy.get("ceiling"), "ceiling"),
        status=_required_text(legacy.get("status"), "status"),
        constraints=tuple(_constraint_view(item) for item in raw_constraints),
    )
    return LegacySeverityResolutionProjection(
        resolution=resolution,
        phase_c_candidate=phase_c_candidate,
    )


def _to_legacy_resolution_for_equivalence(
    projection: LegacySeverityResolutionProjection,
) -> dict[str, Any]:
    """Reconstruct the old dictionary for a test-only losslessness check."""

    if not isinstance(projection, LegacySeverityResolutionProjection):
        raise TypeError("projection must be a LegacySeverityResolutionProjection")

    # Import lazily so the facade remains downstream of the existing business
    # core and never becomes an import dependency of derive.py.
    from ..postprocess.derive import SeverityConstraint

    resolution = projection.resolution
    constraints = tuple(
        SeverityConstraint(
            item.kind,
            item.level,
            item.rule,
            item.reason,
            item.evidence_ids,
        )
        for item in resolution.constraints
    )
    return {
        "severity": resolution.resolved_severity,
        "status": resolution.status,
        "model_severity": resolution.model_severity,
        "floor": resolution.floor,
        "ceiling": resolution.ceiling,
        "constraints": constraints,
        "phase_c_candidate": projection.phase_c_candidate,
    }


def _stage_code(stage: Mapping[str, Any]) -> str | None:
    match = _STAGE_CODE_RE.match(str(stage.get("stage") or "").strip())
    return match.group(1) if match else None


def _stage_resolution_views(
    result: Mapping[str, Any],
) -> list[tuple[str, LegacySeverityResolutionProjection, Mapping[str, Any]]]:
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return []

    views: list[tuple[str, LegacySeverityResolutionProjection, Mapping[str, Any]]] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        code = _stage_code(stage)
        trace = stage.get("severity_derivation")
        if code is None or not isinstance(trace, Mapping):
            continue
        views.append((code, from_legacy_resolution(trace), trace))
    return views


def legacy_provisional_projection(result: Mapping[str, Any]) -> LegacyProvisionalProjection:
    """Project the existing pre-review result without changing it."""

    views = _stage_resolution_views(result)
    candidates = tuple(
        LegacyPhaseCCandidateView(
            stage_id=stage_id,
        )
        for stage_id, projection, _ in views
        if projection.phase_c_candidate
    )
    return LegacyProvisionalProjection(
        severity_resolutions=tuple(projection.resolution for _, projection, _ in views),
        candidate_set=LegacyPhaseCCandidateSet(candidates=candidates),
    )


def legacy_terminal_projection(result: Mapping[str, Any]) -> LegacyTerminalProjection:
    """Project the terminal boundary without exposing legacy candidates."""

    views = _stage_resolution_views(result)
    return LegacyTerminalProjection(
        severity_resolutions=tuple(projection.resolution for _, projection, _ in views),
    )
