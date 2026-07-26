"""PR-0A compatibility equivalence and shadow helpers.

This module is deliberately downstream of the existing LEGACY_V1 path.  It
provides in-memory comparison and tracing for tests and shadow runs; it does
not call an LLM, write artifacts, update run state, or become a production
execution entry point.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from .contracts import (
    LegacyPhaseCCandidateSet,
    LegacyPhaseCCandidateView,
    LegacyProvisionalProjection,
    SeverityResolutionFacade,
)


_MISSING = object()
_ALLOWED_CANONICALIZATION_REASONS = frozenset(
    {"timestamp", "uuid", "temporary_path", "pid", "duration"}
)
_AUDIT_KEYS = (
    "audit",
    "audit_taxonomy",
    "audit_events",
    "postprocess_change_log",
    "postprocess_provenance",
    "quality_audit",
    "qa_warnings",
)
_CONTENT_COMPARISON_NAMES = frozenset(
    {
        "severity",
        "floor_ceiling",
        "constraint",
        "reason_codes",
        "severity_derivation",
        "legacy_phase_c",
    }
)


class UnsupportedCanonicalizationError(ValueError):
    """Raised when canonicalization requests an unapproved exclusion."""


@dataclass(frozen=True, kw_only=True)
class CanonicalizationExclusion:
    """One exact JSON path that may be excluded for a documented reason."""

    json_path: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class CanonicalizationRecord:
    """Evidence that one approved exclusion was applied."""

    json_path: str
    reason: str
    count: int
    side: str


@dataclass(frozen=True, kw_only=True)
class SideEffectTrace:
    """Counts of observable calls and writes recorded by one shadow branch."""

    llm_calls: int = 0
    resolver_calls: int = 0
    phase_c_calls: int = 0
    artifact_writes: int = 0
    state_transitions: int = 0
    cache_updates: int = 0
    manifest_updates: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "resolver_calls": self.resolver_calls,
            "phase_c_calls": self.phase_c_calls,
            "artifact_writes": self.artifact_writes,
            "state_transitions": self.state_transitions,
            "cache_updates": self.cache_updates,
            "manifest_updates": self.manifest_updates,
        }


@dataclass
class ShadowSideEffectRecorder:
    """In-memory recorder supplied to legacy and facade shadow callbacks."""

    _llm_calls: int = 0
    _resolver_calls: int = 0
    _phase_c_calls: int = 0
    _artifact_writes: int = 0
    _state_transitions: int = 0
    _cache_updates: int = 0
    _manifest_updates: int = 0

    def record_llm_call(self) -> None:
        self._llm_calls += 1

    def record_resolver_call(self) -> None:
        self._resolver_calls += 1

    def record_phase_c_call(self) -> None:
        self._phase_c_calls += 1

    def record_artifact_write(self) -> None:
        self._artifact_writes += 1

    def record_state_transition(self) -> None:
        self._state_transitions += 1

    def record_cache_update(self) -> None:
        self._cache_updates += 1

    def record_manifest_update(self) -> None:
        self._manifest_updates += 1

    def snapshot(self) -> SideEffectTrace:
        return SideEffectTrace(
            llm_calls=self._llm_calls,
            resolver_calls=self._resolver_calls,
            phase_c_calls=self._phase_c_calls,
            artifact_writes=self._artifact_writes,
            state_transitions=self._state_transitions,
            cache_updates=self._cache_updates,
            manifest_updates=self._manifest_updates,
        )


@dataclass(frozen=True, kw_only=True)
class ShadowInputs:
    """The one shared input envelope used by both shadow branches."""

    input_value: Any
    raw_response: Any
    config: Mapping[str, Any]
    environment: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class ShadowRunOutput:
    """Output returned by one in-memory shadow callback."""

    output: Mapping[str, Any]
    candidate_set: LegacyPhaseCCandidateSet


ShadowRunner: TypeAlias = Callable[[ShadowInputs, ShadowSideEffectRecorder], ShadowRunOutput]


@dataclass(frozen=True, kw_only=True)
class CompatibilityDifference:
    path: str
    legacy_value: Any
    facade_value: Any


@dataclass(frozen=True, kw_only=True)
class CompatibilityComparison:
    name: str
    passed: bool
    differences: tuple[CompatibilityDifference, ...] = ()
    classification: str = "none"


@dataclass(frozen=True, kw_only=True)
class CompatibilityEquivalenceReport:
    """Structured result suitable for a Commit 2 acceptance report."""

    comparisons: tuple[CompatibilityComparison, ...]
    canonicalization: tuple[CanonicalizationRecord, ...] = ()
    legacy_side_effects: SideEffectTrace = field(default_factory=SideEffectTrace)
    facade_side_effects: SideEffectTrace = field(default_factory=SideEffectTrace)
    input_fingerprint: str | None = None
    shadow_safe: bool = True

    @property
    def passed(self) -> bool:
        return self.shadow_safe and all(item.passed for item in self.comparisons)

    @property
    def comparison_map(self) -> dict[str, CompatibilityComparison]:
        return {item.name: item for item in self.comparisons}

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "shadow_safe": self.shadow_safe,
            "input_fingerprint": self.input_fingerprint,
            "comparisons": {
                item.name: {
                    "passed": item.passed,
                    "classification": item.classification,
                    "differences": [
                        {
                            "path": difference.path,
                            "legacy_value": _display_value(difference.legacy_value),
                            "facade_value": _display_value(difference.facade_value),
                        }
                        for difference in item.differences
                    ],
                }
                for item in self.comparisons
            },
            "canonicalization": [dataclasses.asdict(item) for item in self.canonicalization],
            "side_effects": {
                "legacy": self.legacy_side_effects.as_dict(),
                "facade": self.facade_side_effects.as_dict(),
            },
        }


def _to_json_value(value: Any) -> Any:
    """Convert repository values without sorting or dropping sequence order."""

    if value is _MISSING:
        return _MISSING
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _to_json_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if hasattr(value, "_asdict") and callable(value._asdict):
        return _to_json_value(value._asdict())
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _pointer_tokens(json_path: str) -> tuple[str, ...]:
    if not isinstance(json_path, str) or not json_path.startswith("/") or json_path == "/":
        raise UnsupportedCanonicalizationError(
            "canonicalization paths must be non-root JSON pointers"
        )
    if "*" in json_path:
        raise UnsupportedCanonicalizationError(
            "canonicalization does not support wildcard paths"
        )
    tokens: list[str] = []
    for token in json_path.split("/")[1:]:
        decoded = token.replace("~1", "/").replace("~0", "~")
        if "~" in decoded:
            raise UnsupportedCanonicalizationError(
                f"invalid JSON pointer escape in {json_path!r}"
            )
        tokens.append(decoded)
    return tuple(tokens)


def _remove_json_pointer(value: Any, tokens: Sequence[str]) -> int:
    if not tokens:
        return 0
    token = tokens[0]
    if isinstance(value, dict):
        if token not in value:
            return 0
        if len(tokens) == 1:
            del value[token]
            return 1
        return _remove_json_pointer(value[token], tokens[1:])
    if isinstance(value, list):
        try:
            index = int(token)
        except ValueError:
            return 0
        if index < 0 or index >= len(value):
            return 0
        if len(tokens) == 1:
            value.pop(index)
            return 1
        return _remove_json_pointer(value[index], tokens[1:])
    return 0


def canonicalize_json(
    value: Any,
    *,
    exclusions: Sequence[CanonicalizationExclusion] = (),
    side: str = "value",
) -> tuple[Any, tuple[CanonicalizationRecord, ...]]:
    """Canonicalize a value using only explicit, approved exact paths."""

    canonical = _to_json_value(value)
    records: list[CanonicalizationRecord] = []
    for exclusion in exclusions:
        if not isinstance(exclusion, CanonicalizationExclusion):
            raise TypeError("exclusions must contain CanonicalizationExclusion values")
        if exclusion.reason not in _ALLOWED_CANONICALIZATION_REASONS:
            raise UnsupportedCanonicalizationError(
                f"unsupported canonicalization reason: {exclusion.reason!r}"
            )
        count = _remove_json_pointer(canonical, _pointer_tokens(exclusion.json_path))
        records.append(
            CanonicalizationRecord(
                json_path=exclusion.json_path,
                reason=exclusion.reason,
                count=count,
                side=side,
            )
        )
    return canonical, tuple(records)


def _pointer_escape(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _display_value(value: Any) -> Any:
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, Mapping):
        return {str(key): _display_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_display_value(item) for item in value]
    return value


def _contains_missing(value: Any) -> bool:
    if value is _MISSING:
        return True
    if isinstance(value, Mapping):
        return any(_contains_missing(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_missing(item) for item in value)
    return False


def _diff_values(
    legacy_value: Any,
    facade_value: Any,
    *,
    path: str = "/",
) -> list[CompatibilityDifference]:
    if legacy_value is _MISSING or facade_value is _MISSING:
        if legacy_value is facade_value:
            return []
        return [
            CompatibilityDifference(
                path=path,
                legacy_value=legacy_value,
                facade_value=facade_value,
            )
        ]
    if isinstance(legacy_value, Mapping) and isinstance(facade_value, Mapping):
        differences: list[CompatibilityDifference] = []
        keys = sorted(set(legacy_value) | set(facade_value), key=str)
        for key in keys:
            differences.extend(
                _diff_values(
                    legacy_value.get(key, _MISSING),
                    facade_value.get(key, _MISSING),
                    path=f"{path.rstrip('/')}/{_pointer_escape(key)}",
                )
            )
        return differences
    if isinstance(legacy_value, (list, tuple)) and isinstance(facade_value, (list, tuple)):
        differences = []
        for index in range(max(len(legacy_value), len(facade_value))):
            differences.extend(
                _diff_values(
                    legacy_value[index] if index < len(legacy_value) else _MISSING,
                    facade_value[index] if index < len(facade_value) else _MISSING,
                    path=f"{path.rstrip('/')}/{index}",
                )
            )
        return differences
    if type(legacy_value) is not type(facade_value) or legacy_value != facade_value:
        return [
            CompatibilityDifference(
                path=path,
                legacy_value=legacy_value,
                facade_value=facade_value,
            )
        ]
    return []


def _make_comparison(
    name: str,
    legacy_value: Any,
    facade_value: Any,
    *,
    classification: str | None = None,
) -> CompatibilityComparison:
    normalized_legacy = _to_json_value(legacy_value)
    normalized_facade = _to_json_value(facade_value)
    differences = tuple(_diff_values(normalized_legacy, normalized_facade))
    if not differences:
        return CompatibilityComparison(name=name, passed=True)
    if classification is None:
        if _contains_missing(normalized_legacy) or _contains_missing(normalized_facade):
            classification = "B. projection 丢字段"
        elif name in _CONTENT_COMPARISON_NAMES:
            classification = "A. facade 映射错误"
        else:
            classification = "E. 未知差异"
    return CompatibilityComparison(
        name=name,
        passed=False,
        differences=differences,
        classification=classification,
    )


def _traces(output: Mapping[str, Any]) -> list[Any]:
    stages = output.get("stage_analysis", _MISSING)
    if isinstance(stages, (list, tuple)):
        return [
            stage.get("severity_derivation", _MISSING)
            if isinstance(stage, Mapping)
            else _MISSING
            for stage in stages
        ]
    return [output]


def _trace_fields(output: Mapping[str, Any], fields: Mapping[str, str]) -> Any:
    stages = output.get("stage_analysis", _MISSING)
    if isinstance(stages, (list, tuple)):
        return {
            "stage_analysis": [
                {
                    target: (
                        stage.get("severity_derivation", {}).get(source, _MISSING)
                        if isinstance(stage, Mapping)
                        and isinstance(stage.get("severity_derivation"), Mapping)
                        else _MISSING
                    )
                    for target, source in fields.items()
                }
                for stage in stages
            ]
        }
    return {
        target: output.get(source, _MISSING)
        for target, source in fields.items()
    }


def _constraint_projection(output: Mapping[str, Any]) -> Any:
    return _trace_fields(
        output,
        {
            "constraints": "constraints",
            "constraint_evaluations": "constraint_evaluations",
        },
    )


def _reason_codes_projection(output: Mapping[str, Any]) -> Any:
    traces = _traces(output)
    projected: list[Any] = []
    for trace in traces:
        if not isinstance(trace, Mapping):
            projected.append(_MISSING)
            continue
        raw_evaluations = trace.get("constraint_evaluations", _MISSING)
        if isinstance(raw_evaluations, (list, tuple)):
            evaluations = [
                {
                    "reason_code": item.get("reason_code", _MISSING),
                    "reason": item.get("reason", _MISSING),
                }
                if isinstance(item, Mapping)
                else _MISSING
                for item in raw_evaluations
            ]
        else:
            evaluations = raw_evaluations
        projected.append(
            {
                "reason": trace.get("reason", _MISSING),
                "constraint_evaluations": evaluations,
            }
        )
    if isinstance(output.get("stage_analysis", _MISSING), (list, tuple)):
        return {"stage_analysis": projected}
    return projected[0]


def _severity_derivation_projection(output: Mapping[str, Any]) -> Any:
    traces = _traces(output)
    return {
        "stage_analysis": [_to_json_value(trace) for trace in traces]
    } if isinstance(output.get("stage_analysis", _MISSING), (list, tuple)) else _to_json_value(traces[0])


def _candidate_set_projection(candidate_set: LegacyPhaseCCandidateSet | None) -> Any:
    if candidate_set is None:
        return _MISSING
    if not isinstance(candidate_set, LegacyPhaseCCandidateSet):
        raise TypeError("candidate_set must be a LegacyPhaseCCandidateSet")
    return {
        "policy_version": candidate_set.policy_version,
        "candidates": [
            {
                "stage_id": candidate.stage_id,
                "legacy_source_refs": list(candidate.legacy_source_refs),
            }
            for candidate in candidate_set.candidates
        ],
    }


def _candidate_projection(
    output: Mapping[str, Any],
    candidate_set: LegacyPhaseCCandidateSet | None,
) -> Any:
    legacy_flags = _trace_fields(output, {"phase_c_candidate": "phase_c_candidate"})
    return {
        "legacy_flags": legacy_flags,
        "typed_legacy_view": _candidate_set_projection(candidate_set),
    }


def _audit_projection(output: Mapping[str, Any]) -> Any:
    stages = output.get("stage_analysis", _MISSING)
    stage_values: list[Any] = []
    if isinstance(stages, (list, tuple)):
        for stage in stages:
            if not isinstance(stage, Mapping):
                stage_values.append(_MISSING)
                continue
            trace = stage.get("severity_derivation", _MISSING)
            stage_values.append(
                {
                    "stage": {
                        key: stage.get(key, _MISSING) for key in _AUDIT_KEYS
                    },
                    "severity_derivation": {
                        key: trace.get(key, _MISSING)
                        if isinstance(trace, Mapping)
                        else _MISSING
                        for key in _AUDIT_KEYS
                    },
                }
            )
    return {
        "top_level": {key: output.get(key, _MISSING) for key in _AUDIT_KEYS},
        "stage_analysis": stage_values,
    }


def _canonicalization_signature(
    records: Sequence[CanonicalizationRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "json_path": record.json_path,
            "reason": record.reason,
            "count": record.count,
        }
        for record in records
    ]


def _shadow_is_safe(
    legacy_side_effects: SideEffectTrace,
    facade_side_effects: SideEffectTrace,
) -> bool:
    forbidden = (
        "llm_calls",
        "phase_c_calls",
        "artifact_writes",
        "state_transitions",
        "cache_updates",
        "manifest_updates",
    )
    return all(
        getattr(trace, field_name) == 0
        for trace in (legacy_side_effects, facade_side_effects)
        for field_name in forbidden
    )


def compare_compatibility(
    legacy_output: Mapping[str, Any],
    facade_output: Mapping[str, Any],
    *,
    legacy_candidate_set: LegacyPhaseCCandidateSet | None = None,
    facade_candidate_set: LegacyPhaseCCandidateSet | None = None,
    legacy_side_effects: SideEffectTrace | None = None,
    facade_side_effects: SideEffectTrace | None = None,
    exclusions: Sequence[CanonicalizationExclusion] = (),
    input_fingerprint: str | None = None,
    shadow_safe: bool = True,
) -> CompatibilityEquivalenceReport:
    """Compare the required PR-0A surfaces without ignoring business fields."""

    if not isinstance(legacy_output, Mapping) or not isinstance(facade_output, Mapping):
        raise TypeError("legacy_output and facade_output must be mappings")

    legacy_side_effects = legacy_side_effects or SideEffectTrace()
    facade_side_effects = facade_side_effects or SideEffectTrace()

    legacy_canonical, legacy_records = canonicalize_json(
        legacy_output,
        exclusions=exclusions,
        side="legacy",
    )
    facade_canonical, facade_records = canonicalize_json(
        facade_output,
        exclusions=exclusions,
        side="facade",
    )
    canonicalization_records = (*legacy_records, *facade_records)
    canonicalization_mismatch = _canonicalization_signature(
        legacy_records
    ) != _canonicalization_signature(facade_records)

    comparisons = [
        _make_comparison(
            "severity",
            _trace_fields(
                legacy_output,
                {
                    "model_severity": "model_severity",
                    "resolved_severity": "severity",
                    "floor": "floor",
                    "ceiling": "ceiling",
                    "resolution_status": "status",
                },
            ),
            _trace_fields(
                facade_output,
                {
                    "model_severity": "model_severity",
                    "resolved_severity": "severity",
                    "floor": "floor",
                    "ceiling": "ceiling",
                    "resolution_status": "status",
                },
            ),
        ),
        _make_comparison(
            "floor_ceiling",
            _trace_fields(legacy_output, {"floor": "floor", "ceiling": "ceiling"}),
            _trace_fields(facade_output, {"floor": "floor", "ceiling": "ceiling"}),
        ),
        _make_comparison(
            "constraint",
            _constraint_projection(legacy_output),
            _constraint_projection(facade_output),
        ),
        _make_comparison(
            "reason_codes",
            _reason_codes_projection(legacy_output),
            _reason_codes_projection(facade_output),
        ),
        _make_comparison(
            "severity_derivation",
            _severity_derivation_projection(legacy_output),
            _severity_derivation_projection(facade_output),
        ),
        _make_comparison(
            "legacy_phase_c",
            _candidate_projection(legacy_output, legacy_candidate_set),
            _candidate_projection(facade_output, facade_candidate_set),
        ),
        _make_comparison(
            "audit",
            _audit_projection(legacy_output),
            _audit_projection(facade_output),
        ),
    ]

    final_json = _make_comparison("final_json", legacy_canonical, facade_canonical)
    if canonicalization_mismatch:
        canonicalization_difference = CompatibilityDifference(
            path="/canonicalization",
            legacy_value=_canonicalization_signature(legacy_records),
            facade_value=_canonicalization_signature(facade_records),
        )
        final_json = CompatibilityComparison(
            name="final_json",
            passed=False,
            differences=(*final_json.differences, canonicalization_difference),
            classification="C. canonicalization 错误",
        )
    comparisons.append(final_json)

    comparisons.append(
        _make_comparison(
            "side_effects",
            legacy_side_effects.as_dict(),
            facade_side_effects.as_dict(),
        )
    )
    return CompatibilityEquivalenceReport(
        comparisons=tuple(comparisons),
        canonicalization=canonicalization_records,
        legacy_side_effects=legacy_side_effects,
        facade_side_effects=facade_side_effects,
        input_fingerprint=input_fingerprint,
        shadow_safe=shadow_safe,
    )


def _input_fingerprint(inputs: ShadowInputs) -> str:
    normalized = _to_json_value(
        {
            "input_value": inputs.input_value,
            "raw_response": inputs.raw_response,
            "config": inputs.config,
            "environment": inputs.environment,
        }
    )
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ShadowSafetyError(RuntimeError):
    """Raised when a shadow callback mutates the shared input envelope."""


def run_shadow(
    inputs: ShadowInputs,
    legacy_runner: ShadowRunner,
    facade_runner: ShadowRunner,
    *,
    exclusions: Sequence[CanonicalizationExclusion] = (),
) -> CompatibilityEquivalenceReport:
    """Run both callbacks against one input envelope and compare in memory."""

    if not isinstance(inputs, ShadowInputs):
        raise TypeError("inputs must be a ShadowInputs instance")
    fingerprint = _input_fingerprint(inputs)

    legacy_recorder = ShadowSideEffectRecorder()
    legacy_result = legacy_runner(inputs, legacy_recorder)
    if _input_fingerprint(inputs) != fingerprint:
        raise ShadowSafetyError("legacy shadow callback mutated shared inputs")

    facade_recorder = ShadowSideEffectRecorder()
    facade_result = facade_runner(inputs, facade_recorder)
    if _input_fingerprint(inputs) != fingerprint:
        raise ShadowSafetyError("facade shadow callback mutated shared inputs")

    if not isinstance(legacy_result, ShadowRunOutput):
        raise TypeError("legacy_runner must return ShadowRunOutput")
    if not isinstance(facade_result, ShadowRunOutput):
        raise TypeError("facade_runner must return ShadowRunOutput")

    legacy_side_effects = legacy_recorder.snapshot()
    facade_side_effects = facade_recorder.snapshot()
    return compare_compatibility(
        legacy_result.output,
        facade_result.output,
        legacy_candidate_set=legacy_result.candidate_set,
        facade_candidate_set=facade_result.candidate_set,
        legacy_side_effects=legacy_side_effects,
        facade_side_effects=facade_side_effects,
        exclusions=exclusions,
        input_fingerprint=fingerprint,
        shadow_safe=_shadow_is_safe(legacy_side_effects, facade_side_effects),
    )


def _constraint_to_legacy_dict(constraint: Any) -> dict[str, Any]:
    if isinstance(constraint, Mapping):
        read = constraint.get
    else:
        read = lambda key, default=None: getattr(constraint, key, default)
    evidence_ids = read("evidence_ids", ()) or ()
    return {
        "kind": read("kind"),
        "level": read("level"),
        "rule": read("rule"),
        "reason": read("reason"),
        "evidence_ids": list(evidence_ids),
    }


def _resolution_to_legacy_fields(
    resolution: SeverityResolutionFacade,
    *,
    phase_c_candidate: bool,
) -> dict[str, Any]:
    return {
        "severity": resolution.resolved_severity,
        "status": resolution.status,
        "model_severity": resolution.model_severity,
        "floor": resolution.floor,
        "ceiling": resolution.ceiling,
        "constraints": [
            _constraint_to_legacy_dict(constraint)
            for constraint in resolution.constraints
        ],
        "phase_c_candidate": phase_c_candidate,
    }


def materialize_legacy_provisional_projection(
    legacy_result: Mapping[str, Any],
    projection: LegacyProvisionalProjection,
) -> dict[str, Any]:
    """Materialize a typed projection for a test-only JSON comparison.

    Only the existing resolver fields and the existing legacy candidate flag
    are replaced.  All other result fields remain copied from the legacy
    result, including reason, audit, and postprocess change-log data.
    """

    if not isinstance(legacy_result, Mapping):
        raise TypeError("legacy_result must be a mapping")
    if not isinstance(projection, LegacyProvisionalProjection):
        raise TypeError("projection must be a LegacyProvisionalProjection")

    materialized = copy.deepcopy(dict(legacy_result))
    candidate_ids = {candidate.stage_id for candidate in projection.candidate_set.candidates}
    resolutions = projection.severity_resolutions
    resolution_index = 0
    stages = materialized.get("stage_analysis", _MISSING)
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            trace = stage.get("severity_derivation")
            if not isinstance(trace, Mapping):
                continue
            if resolution_index >= len(resolutions):
                raise ValueError("projection has fewer resolutions than legacy traces")
            stage_id = str(stage.get("stage") or "").strip().split(maxsplit=1)[0]
            resolution = resolutions[resolution_index]
            trace_copy = dict(trace)
            trace_copy.update(
                _resolution_to_legacy_fields(
                    resolution,
                    phase_c_candidate=stage_id in candidate_ids,
                )
            )
            stage["severity_derivation"] = trace_copy
            resolution_index += 1
        if resolution_index != len(resolutions):
            raise ValueError("projection has more resolutions than legacy traces")
        return materialized

    if resolutions:
        if len(resolutions) != 1:
            raise ValueError("direct legacy result requires exactly one resolution")
        materialized.update(
            _resolution_to_legacy_fields(
                resolutions[0],
                phase_c_candidate=bool(projection.candidate_set.candidates),
            )
        )
    return materialized
