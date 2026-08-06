from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.summarize_s4_structure_experiment import summarize_variant


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _gt() -> dict:
    return {
        "samples": {
            "sample-large": {"human_gap": {"S4": "large"}},
            "sample-none": {"human_gap": {"S4": "none"}},
            "sample-failed": {"human_gap": {"S4": "large"}},
        }
    }


def _success(gap: str) -> dict:
    return {
        "status": "completed",
        "result": {
            "stage": "S4 效果呈现",
            "relation": "benchmark_better" if gap != "none" else "tie",
            "gap_magnitude": gap,
        },
    }


class SummarizeS4StructureExperimentTests(unittest.TestCase):
    def test_keeps_gap_scoring_and_operational_failures_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gt_path = root / "gt.json"
            _write_json(gt_path, _gt())
            artifact_root = root / "single-pass"
            state_root = root / "state"
            model = "qwen3.6-plus"
            _write_json(
                artifact_root / "sample-large" / model / "s4_single_pass_evaluation.json",
                _success("large"),
            )
            _write_json(
                artifact_root / "sample-none" / model / "s4_single_pass_evaluation.json",
                _success("none"),
            )
            _write_json(
                artifact_root / "sample-failed" / model / "s4_single_pass_failure.json",
                {"status": "contract_failed", "failure_class": "contract_validation"},
            )

            summary = summarize_variant(
                sample_ids=["sample-large", "sample-none", "sample-failed"],
                gt_path=gt_path,
                root=artifact_root,
                model=model,
                variant="single_pass",
            )

        metrics = summary["metrics"]
        self.assertEqual(metrics["human_labeled_s4_cells"], 3)
        self.assertEqual(metrics["completed_artifacts"], 2)
        self.assertEqual(metrics["contract_failed_artifacts"], 1)
        self.assertEqual(metrics["gap_accuracy_among_scorable_outputs"], 1.0)
        self.assertEqual(metrics["end_to_end_gap_accuracy"], 0.666667)
        self.assertEqual(metrics["end_to_end_gt_large_recall"], 0.5)
        self.assertEqual(metrics["relation_labeled_cells"], 0)
        self.assertIsNone(metrics["relation_accuracy"])
        self.assertIn("no frozen stage_relations", summary["notes"]["relation_accuracy"])

    def test_locked_state_marks_failed_first_step_as_a_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gt_path = root / "gt.json"
            _write_json(gt_path, _gt())
            fact_state_root = root / "fact-state"
            _write_json(
                fact_state_root / "sample-large" / "qwen3.6-plus" / "s4_fact_state_failure.json",
                {"status": "contract_failed", "failure_class": "contract_validation"},
            )

            summary = summarize_variant(
                sample_ids=["sample-large"],
                gt_path=gt_path,
                root=root / "judgment",
                model="qwen3.6-plus",
                variant="locked_state",
                fact_state_root=fact_state_root,
            )

        self.assertEqual(summary["metrics"]["blocked_fact_state_artifacts"], 1)
        self.assertEqual(summary["metrics"]["missing_artifacts"], 0)
        self.assertEqual(summary["rows"][0]["artifact_status"], "blocked_fact_state")
        self.assertEqual(summary["rows"][0]["fact_state_status"], "contract_failed")


if __name__ == "__main__":
    unittest.main()
