from __future__ import annotations

import unittest

from scripts.evaluate_analysis import severity_diagnostics, severity_from_score


class SeverityEvaluationDiagnosticsTest(unittest.TestCase):
    def test_score_bucket_uses_derive_boundaries(self) -> None:
        self.assertEqual(severity_from_score(1.2), "small")
        self.assertEqual(severity_from_score(1.21), "medium")
        self.assertEqual(severity_from_score(2.5), "medium")
        self.assertEqual(severity_from_score(2.51), "large")

    def test_near_threshold_mismatch_is_diagnostic_only(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "S": 1.15,
                    "reason": "E = 标杆执行分 1.0 - 达人执行分 0.5",
                }
            },
        )
        self.assertEqual(diagnostics["ordinal_distance"], 1)
        self.assertEqual(diagnostics["derivation_path"], "threshold")
        self.assertTrue(diagnostics["near_threshold"])

    def test_floor_override_is_not_explained_as_boundary_noise(self) -> None:
        diagnostics = severity_diagnostics(
            "small",
            "medium",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "medium",
                    "S": 0.9,
                    "reason": "landing 下限：标杆钩子立住、达人未立住",
                }
            },
        )
        self.assertEqual(diagnostics["ordinal_distance"], 1)
        self.assertEqual(diagnostics["derivation_path"], "override_or_floor")
        self.assertIsNone(diagnostics["near_threshold"])

    def test_two_band_error_is_preserved(self) -> None:
        diagnostics = severity_diagnostics("large", "small", {})
        self.assertEqual(diagnostics["ordinal_distance"], 2)
        self.assertEqual(diagnostics["derivation_path"], "non_score_path")
        self.assertEqual(diagnostics["decision_mechanism"], "other_non_score_path")

    def test_non_positive_gap_is_not_classified_as_missing_evidence(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "E": 0,
                    "reason": "E = 标杆执行分 2.0 - 达人执行分 2.0；达人持平或更优（亮点，零差距红线）",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "non_positive_execution_gap")

    def test_both_absent_has_its_own_mechanism(self) -> None:
        diagnostics = severity_diagnostics(
            "large",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "E": 0,
                    "reason": "双方均未涉及（执行分均为 0），不进公式",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "both_absent")

    def test_non_score_floor_is_classified_as_structural_override(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "medium",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "medium",
                    "E": 0,
                    "reason": "E = 标杆执行分 2.0 - 达人执行分 2.0；命题锚下限：标杆锚定核心命题",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "structural_override")


if __name__ == "__main__":
    unittest.main()
