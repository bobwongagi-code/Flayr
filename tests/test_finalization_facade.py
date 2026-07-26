from __future__ import annotations

import copy
import dataclasses
import unittest

from scripts.flayr_core.finalization import (
    LegacyPhaseCCandidateSet,
    LegacyTerminalProjection,
    SeverityResolutionFacade,
    from_legacy_resolution,
    legacy_provisional_projection,
    legacy_terminal_projection,
)
from scripts.flayr_core.finalization.facade import _to_legacy_resolution_for_equivalence
from scripts.flayr_core.postprocess.derive import SeverityConstraint, resolve_severity


class FinalizationFacadeTests(unittest.TestCase):
    def test_legacy_resolution_projection_is_lossless(self) -> None:
        legacy = resolve_severity(
            "small",
            (SeverityConstraint("floor", "large", "floor_rule", "floor reason", ("C1",)),),
            (SeverityConstraint("ceiling", "medium", "ceiling_rule", "ceiling reason", ("B1",)),),
        )
        before = copy.deepcopy(legacy)

        projected = from_legacy_resolution(legacy)
        restored = _to_legacy_resolution_for_equivalence(projected)

        self.assertEqual(restored, legacy)
        self.assertEqual(legacy, before)
        self.assertIsInstance(projected.resolution, SeverityResolutionFacade)

    def test_legacy_facade_projection_does_not_mutate_result(self) -> None:
        result = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "severity_derivation": {
                        "severity": "small",
                        "status": "conflict",
                        "model_severity": "small",
                        "floor": "large",
                        "ceiling": "medium",
                        "constraints": [
                            {
                                "kind": "floor",
                                "level": "large",
                                "rule": "floor_rule",
                                "reason": "floor reason",
                                "evidence_ids": ["C1"],
                            }
                        ],
                        "phase_c_candidate": True,
                    },
                }
            ]
        }
        before = copy.deepcopy(result)

        provisional = legacy_provisional_projection(result)
        terminal = legacy_terminal_projection(result)

        self.assertEqual(result, before)
        self.assertEqual(provisional.candidate_set.candidates[0].stage_id, "S1")
        self.assertEqual(provisional.candidate_set.candidates[0].legacy_source_refs, ())
        self.assertEqual(len(terminal.severity_resolutions), 1)

    def test_legacy_candidate_set_is_explicitly_legacy_v1(self) -> None:
        candidate_set = LegacyPhaseCCandidateSet()

        self.assertEqual(candidate_set.policy_version, "legacy-v1")
        self.assertEqual(candidate_set.candidates, ())

    def test_terminal_projection_has_no_candidate_field(self) -> None:
        field_names = {field.name for field in dataclasses.fields(LegacyTerminalProjection)}

        self.assertNotIn("candidate_set", field_names)
        self.assertNotIn("phase_c_candidates", field_names)
        self.assertNotIn("phase_c_plan_request", field_names)
