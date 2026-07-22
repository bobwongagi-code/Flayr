"""Regression tests for the versioned analysis-result domain boundary."""

from __future__ import annotations

import unittest

from scripts.flayr_core.analysis_model import (
    ANALYSIS_RESULT_CONTRACT,
    ANALYSIS_PROJECTION_FIELDS,
    AnalysisResult,
    placeholder_stages,
)


class AnalysisModelContractTests(unittest.TestCase):
    def test_runtime_contract_is_backed_by_the_model_schema(self) -> None:
        self.assertEqual(ANALYSIS_RESULT_CONTRACT.stage_count, 6)
        for field in ANALYSIS_RESULT_CONTRACT.normalized_required_fields:
            self.assertIn(field, ANALYSIS_RESULT_CONTRACT.schema_fields)
        self.assertEqual(tuple(ANALYSIS_PROJECTION_FIELDS), ANALYSIS_RESULT_CONTRACT.projection_fields)

    def test_projection_is_the_only_result_to_runtime_boundary(self) -> None:
        normalized = {
            "executive_summary": "结论",
            "one_line_summary": "结论",
            "stage_analysis": [{"stage": "S1 Hook"}],
            "improvements": [{"title": "改进"}],
            "global_diagnosis": {"overall_status": "major"},
            "unowned_model_field": "must not leak into runtime projection",
        }
        analysis: dict[str, object] = {"videos": {"benchmark": {}}}

        AnalysisResult.from_mapping(normalized).project_into(analysis)

        self.assertEqual(analysis["executive_summary"], "结论")
        self.assertEqual(analysis["global_diagnosis"], {"overall_status": "major"})
        self.assertNotIn("unowned_model_field", analysis)
        self.assertIn("analysis_result_contract", analysis)
        self.assertEqual(
            analysis["analysis_result_contract"]["version"],
            ANALYSIS_RESULT_CONTRACT.version,
        )

        # Runtime projection must not alias mutable model output.
        normalized["stage_analysis"][0]["stage"] = "mutated after projection"
        self.assertEqual(analysis["stage_analysis"][0]["stage"], "S1 Hook")

    def test_lifecycle_metadata_declares_input_output_and_version(self) -> None:
        metadata = ANALYSIS_RESULT_CONTRACT.metadata()
        lifecycle = metadata["lifecycle"]
        self.assertEqual(
            [phase["name"] for phase in lifecycle],
            ["raw_model_response", "validated_normalized_result", "final_derived_result"],
        )
        for phase in lifecycle:
            self.assertGreaterEqual(phase["version"], 1)
            self.assertTrue(phase["input_artifact"])
            self.assertTrue(phase["output_artifact"])
        self.assertEqual(len(metadata["schema_sha256"]), 64)

    def test_placeholder_stages_are_owned_by_the_same_stage_catalog(self) -> None:
        stages = placeholder_stages()
        self.assertEqual(len(stages), ANALYSIS_RESULT_CONTRACT.stage_count)
        self.assertEqual([stage["stage"][:2] for stage in stages], [f"S{i}" for i in range(1, 7)])
        self.assertTrue(all(stage["placeholder"] is True for stage in stages))


if __name__ == "__main__":
    unittest.main()
