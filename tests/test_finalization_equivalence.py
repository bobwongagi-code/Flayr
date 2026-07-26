from __future__ import annotations

import copy
import unittest

from scripts.flayr_core.finalization import (
    LegacyPhaseCCandidateSet,
    LegacyPhaseCCandidateView,
    LegacyProvisionalProjection,
    from_legacy_resolution,
    legacy_provisional_projection,
)
from scripts.flayr_core.finalization.equivalence import (
    CanonicalizationExclusion,
    ShadowInputs,
    ShadowRunOutput,
    SideEffectTrace,
    UnsupportedCanonicalizationError,
    canonicalize_json,
    compare_compatibility,
    materialize_legacy_provisional_projection,
    run_shadow,
)
from scripts.flayr_core.postprocess.derive import SeverityConstraint, resolve_severity


def _resolution() -> dict[str, object]:
    return resolve_severity(
        "small",
        (SeverityConstraint("floor", "large", "floor_rule", "floor reason", ("C1",)),),
        (SeverityConstraint("ceiling", "medium", "ceiling_rule", "ceiling reason", ("B1",)),),
    )


def _trace(resolution: dict[str, object]) -> dict[str, object]:
    constraints = resolution["constraints"]
    return {
        "status": resolution["status"],
        "severity": resolution["severity"],
        "model_severity": resolution["model_severity"],
        "resolver": "floor_ceiling_v1",
        "floor": resolution["floor"],
        "ceiling": resolution["ceiling"],
        "constraints": [
            {
                "kind": item.kind,
                "level": item.level,
                "rule": item.rule,
                "reason": item.reason,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in constraints
        ],
        "constraint_evaluations": [
            {
                "rule": "floor_rule",
                "status": "triggered",
                "reason_code": "constraint_applied",
                "reason": "floor reason",
            },
            {
                "rule": "ceiling_rule",
                "status": "triggered",
                "reason_code": "constraint_applied",
                "reason": "ceiling reason",
            },
        ],
        "phase_c_candidate": resolution["phase_c_candidate"],
        "reason": "floor 与 ceiling 冲突，保留模型 severity，交 Phase C 复核。",
        "audit_taxonomy": ["severity_resolution", "legacy_phase_c_candidate"],
        "postprocess_change_log": [
            {"path": "/stage_analysis/0/severity", "action": "legacy_preserved"}
        ],
    }


def _raw_response() -> dict[str, object]:
    return {
        "metadata": {
            "timestamp": "2026-07-26T00:00:00Z",
            "uuid": "run-uuid",
            "duration": 1.25,
        },
        "stage_analysis": [{"stage": "S1 Hook"}],
        "audit_taxonomy": ["legacy_v1"],
        "postprocess_change_log": [
            {"path": "/stage_analysis/0/severity", "action": "legacy_preserved"}
        ],
    }


def _legacy_result(raw_response: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(raw_response)
    result["stage_analysis"][0]["severity_derivation"] = _trace(_resolution())
    return result


class FinalizationEquivalenceTests(unittest.TestCase):
    def test_severity_equivalence_includes_resolution_fields(self) -> None:
        legacy = _resolution()
        facade = from_legacy_resolution(legacy).resolution
        legacy_result = {
            "model_severity": legacy["model_severity"],
            "severity": legacy["severity"],
            "floor": legacy["floor"],
            "ceiling": legacy["ceiling"],
            "status": legacy["status"],
            "constraints": legacy["constraints"],
            "phase_c_candidate": legacy["phase_c_candidate"],
        }
        facade_result = {
            "model_severity": facade.model_severity,
            "severity": facade.resolved_severity,
            "floor": facade.floor,
            "ceiling": facade.ceiling,
            "status": facade.status,
            "constraints": facade.constraints,
            "phase_c_candidate": legacy["phase_c_candidate"],
        }

        report = compare_compatibility(legacy_result, facade_result)

        self.assertTrue(report.comparison_map["severity"].passed)
        self.assertTrue(report.comparison_map["floor_ceiling"].passed)

    def test_legacy_phase_c_candidate_projection_is_equivalent(self) -> None:
        legacy = _legacy_result(_raw_response())
        projection = legacy_provisional_projection(legacy)
        facade_output = materialize_legacy_provisional_projection(legacy, projection)

        report = compare_compatibility(
            legacy,
            facade_output,
            legacy_candidate_set=projection.candidate_set,
            facade_candidate_set=projection.candidate_set,
        )

        self.assertTrue(report.comparison_map["legacy_phase_c"].passed)
        self.assertEqual(
            projection.candidate_set.candidates,
            (LegacyPhaseCCandidateView(stage_id="S1"),),
        )
        self.assertEqual(projection.candidate_set.policy_version, "legacy-v1")

    def test_canonical_json_equivalence_records_only_approved_exclusions(self) -> None:
        legacy = _legacy_result(_raw_response())
        facade = copy.deepcopy(legacy)
        facade["metadata"]["timestamp"] = "2026-07-26T00:00:01Z"
        facade["metadata"]["uuid"] = "another-uuid"
        facade["metadata"]["duration"] = 9.5
        exclusions = (
            CanonicalizationExclusion(
                json_path="/metadata/timestamp",
                reason="timestamp",
            ),
            CanonicalizationExclusion(json_path="/metadata/uuid", reason="uuid"),
            CanonicalizationExclusion(
                json_path="/metadata/duration",
                reason="duration",
            ),
        )

        report = compare_compatibility(legacy, facade, exclusions=exclusions)

        self.assertTrue(report.comparison_map["final_json"].passed)
        self.assertEqual(len(report.canonicalization), 6)
        self.assertEqual(
            [(item.json_path, item.reason, item.count) for item in report.canonicalization],
            [
                ("/metadata/timestamp", "timestamp", 1),
                ("/metadata/uuid", "uuid", 1),
                ("/metadata/duration", "duration", 1),
            ]
            * 2,
        )
        with self.assertRaises(UnsupportedCanonicalizationError):
            canonicalize_json(
                legacy,
                exclusions=(
                    CanonicalizationExclusion(
                        json_path="/stage_analysis/*/severity",
                        reason="temporary_path",
                    ),
                ),
            )
        with self.assertRaises(UnsupportedCanonicalizationError):
            canonicalize_json(
                legacy,
                exclusions=(
                    CanonicalizationExclusion(
                        json_path="/metadata/timestamp",
                        reason="business_field",
                    ),
                ),
            )

    def test_shadow_reuses_one_input_envelope_and_has_no_formal_side_effects(self) -> None:
        raw_response = _raw_response()
        inputs = ShadowInputs(
            input_value={"model_severity": "small", "floor": "large", "ceiling": "medium"},
            raw_response=raw_response,
            config={"resolver": "legacy-v1"},
            environment={"FLAYR_MODE": "test"},
        )
        seen_input_ids: list[int] = []

        def legacy_runner(
            received: ShadowInputs,
            recorder,
        ) -> ShadowRunOutput:
            seen_input_ids.append(id(received))
            recorder.record_resolver_call()
            result = _legacy_result(received.raw_response)
            projection = legacy_provisional_projection(result)
            return ShadowRunOutput(output=result, candidate_set=projection.candidate_set)

        def facade_runner(
            received: ShadowInputs,
            recorder,
        ) -> ShadowRunOutput:
            seen_input_ids.append(id(received))
            recorder.record_resolver_call()
            result = _legacy_result(received.raw_response)
            projection = legacy_provisional_projection(result)
            facade_result = materialize_legacy_provisional_projection(result, projection)
            return ShadowRunOutput(output=facade_result, candidate_set=projection.candidate_set)

        before = copy.deepcopy(inputs.raw_response)
        report = run_shadow(inputs, legacy_runner, facade_runner)

        self.assertTrue(report.passed)
        self.assertEqual(seen_input_ids, [id(inputs), id(inputs)])
        self.assertEqual(inputs.raw_response, before)
        self.assertEqual(report.input_fingerprint and len(report.input_fingerprint), 64)
        self.assertEqual(report.legacy_side_effects, report.facade_side_effects)
        self.assertEqual(
            report.legacy_side_effects,
            SideEffectTrace(resolver_calls=1),
        )
        self.assertTrue(report.shadow_safe)
        self.assertEqual(report.comparison_map["side_effects"].passed, True)

    def test_unexplained_business_difference_is_reported_without_rewriting_logic(self) -> None:
        legacy = _legacy_result(_raw_response())
        facade = copy.deepcopy(legacy)
        facade["stage_analysis"][0]["severity_derivation"]["severity"] = "medium"
        before = copy.deepcopy(legacy)

        report = compare_compatibility(legacy, facade)

        self.assertFalse(report.passed)
        self.assertFalse(report.comparison_map["severity"].passed)
        self.assertEqual(report.comparison_map["severity"].classification, "A. facade 映射错误")
        self.assertTrue(any("resolved_severity" in item.path for item in report.comparison_map["severity"].differences))
        self.assertEqual(legacy, before)
