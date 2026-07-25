from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core.evidence_states import evidence_strength_gate_report
from scripts.flayr_core.offline_replay import replay_derive_result, replay_many


class OfflineReplayTests(unittest.TestCase):
    def _result(self) -> dict:
        return {
            "mode": "compare",
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "model_severity": "small",
                    "severity": "small",
                },
                {
                    "stage": "S2 Intro",
                    "model_severity": "medium",
                    "severity": "medium",
                },
            ],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1"}]},
                "benchmark": {"evidence_units": [{"id": "B1"}]},
            },
        }

    def test_replay_is_offline_and_preserves_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "analysis.json"
            source.write_text(json.dumps(self._result()), encoding="utf-8")
            replayed = replay_derive_result(self._result(), source)
            metadata = replayed["offline_derive_replay"]
            self.assertEqual(metadata["mode"], "offline_derive_only")
            self.assertEqual(metadata["api_calls"], 0)
            self.assertEqual(metadata["source"]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(metadata["evidence_strength"]["status"], "gate_closed")
            self.assertIn("severity_derivation", replayed["stage_analysis"][0])

    def test_replay_separates_model_alignment_from_constraint_changes(self) -> None:
        result = self._result()
        result["stage_analysis"][0]["model_severity"] = "large"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "analysis.json"
            source.write_text(json.dumps(result), encoding="utf-8")
            replayed = replay_derive_result(result, source)
            summary = replayed["offline_derive_replay"]["summary"]
            self.assertEqual(summary["historical_final_to_replay"]["changed_stage_count"], 1)
            self.assertEqual(summary["historical_final_to_replay"]["severity_increases"], 1)
            self.assertEqual(summary["model_to_replay"]["changed_stage_count"], 0)

    def test_replay_attributes_single_rule_effect_against_model_baseline(self) -> None:
        result = {
            "mode": "compare",
            "stage_analysis": [{
                "stage": "S6 CTA",
                "model_severity": "large",
                "severity": "large",
                "creator_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "action_path_clear": True,
                    "evidence_ids": ["C6"],
                },
                "benchmark_s6": {"exists": False, "evidence_ids": ["B6"]},
            }],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C6"}]},
                "benchmark": {"evidence_units": [{"id": "B6"}]},
            },
        }
        replayed = replay_derive_result(result)
        metadata = replayed["offline_derive_replay"]
        self.assertEqual(metadata["summary"]["model_to_replay"]["changed_stage_count"], 1)
        effect = metadata["resolver_effects"]["direct_rule_effects"]["S6_creator_cta_ceiling"]
        self.assertEqual(effect["changed_stage_count"], 1)
        self.assertEqual(effect["severity_decreases"], 1)

    def test_replay_many_returns_a_summary_without_provider_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for sample_id in ("sample-a", "sample-b"):
                sample = root / sample_id
                sample.mkdir()
                path = sample / "analysis.json"
                path.write_text(json.dumps(self._result()), encoding="utf-8")
                paths.append(path)
            report = replay_many(paths)
            self.assertEqual(report["api_calls"], 0)
            self.assertEqual(report["summary"]["inputs"], 2)
            self.assertEqual([item["sample_id"] for item in report["records"]], ["a", "b"])
            self.assertIn("result", report["records"][0])

    def test_evidence_strength_missing_is_a_closed_gate_not_absent(self) -> None:
        report = evidence_strength_gate_report(self._result())
        self.assertEqual(report["status"], "gate_closed")
        self.assertEqual(report["sides"]["creator"]["missing_strength_ids"], ["C1"])
        self.assertNotEqual(report["sides"]["creator"]["status"], "absent")

    def test_malformed_evidence_units_close_the_gate(self) -> None:
        result = self._result()
        result["video_understanding"]["creator"]["evidence_units"] = [{"evidence_strength": "direct"}, "bad"]
        report = evidence_strength_gate_report(result)
        creator = report["sides"]["creator"]
        self.assertEqual(creator["status"], "gate_closed")
        self.assertEqual(creator["invalid_unit_count"], 2)
        self.assertIn("invalid_evidence_unit", creator["structural_errors"])


if __name__ == "__main__":
    unittest.main()
