from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.flayr_core.validation_cohort import (
    build_cohort_lock,
    spend_cohort_lock,
    sha256_file,
    stage_label_status,
    validate_blind_sample_contract,
    verify_cohort_lock,
)


class ValidationCohortTest(unittest.TestCase):
    def _use_validation_root(self, root: Path) -> None:
        patcher = patch.dict(os.environ, {"FLAYR_VALIDATION_ROOT": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _model_config(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider": "test-provider",
            "api_url": "https://example.invalid",
            "model": "future-model",
            "fallback_model": None,
            "temperature": 0.0,
            "max_tokens": 16384,
            "top_p": None,
            "seed": None,
            "response_format": None,
            "stop": None,
            "transport_retry": 2,
            "completion_attempts": 3,
            "timeout": {"connect": 30, "read": None, "low_speed": 180, "overall": 1800},
        }

    def _label(self) -> dict:
        stages = {f"S{index}": "small" for index in range(1, 7)}
        events = [
            {
                "id": f"{stage.lower()}_decision",
                "role": "creator",
                "stage": stage,
                "time_range": [0.0, 1.0],
                "channels_any": ["visual_fact"],
                "expected_state": "present",
            }
            for stage in stages
        ]
        oracles = {
            stage: {
                "creator_execution": 1.0,
                "benchmark_execution": 1.0,
                "relation": "matched",
                "decision_event_ids": [f"{stage.lower()}_decision"],
                "reason": "双方执行相当",
                "confidence": "high",
            }
            for stage in stages
        }
        return {
            "partition": "blind",
            "stages": stages,
            "stage_oracles": oracles,
            "key_events": events,
            "decision_gt": {
                "top_root_causes": [{
                    "priority": 1,
                    "reference_id": "S1",
                    "reason": "首要改进在 S1",
                    "evidence_event_ids": ["s1_decision"],
                }]
            },
        }

    def test_blind_contract_requires_stage_oracles_and_decision_gt(self) -> None:
        sample = {"group": "blind"}
        self.assertEqual(validate_blind_sample_contract("sample", self._label(), sample), [])
        broken = self._label()
        broken["stage_oracles"].pop("S4")
        broken.pop("decision_gt")
        errors = validate_blind_sample_contract("sample", broken, sample)
        self.assertTrue(any("S4" in error for error in errors))
        self.assertTrue(any("top_root_causes" in error for error in errors))

    def test_blind_na_requires_explicit_not_applicable_reason(self) -> None:
        label = self._label()
        label["stages"]["S5"] = "na"
        label["stage_oracles"].pop("S5")
        errors = validate_blind_sample_contract("sample", label, {"group": "blind"})
        self.assertTrue(any("not_applicable" in error for error in errors))
        label["stage_label_statuses"] = {
            "S5": {"status": "not_applicable", "reason": "此视频不涉及独立信任放大。"}
        }
        self.assertEqual(validate_blind_sample_contract("sample", label, {"group": "blind"}), [])
        self.assertEqual(stage_label_status(label, "S5"), ("not_applicable", "此视频不涉及独立信任放大。"))

    def test_lock_detects_drift_and_can_be_spent(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            creator = root / "creator.mp4"
            benchmark = root / "benchmark.mp4"
            creator.write_bytes(b"creator")
            benchmark.write_bytes(b"benchmark")
            self._use_validation_root(root)
            labels_path = root / "labels.json"
            manifest_path = root / "manifest.json"
            labels_path.write_text(json.dumps({"samples": {"sample": self._label()}}), encoding="utf-8")
            manifest_path.write_text(json.dumps({"samples": [{
                "id": "sample",
                "group": "blind",
                "product_category": "test",
                "target_market": "th",
                "creator_video": str(creator),
                "benchmark_video": str(benchmark),
            }]}), encoding="utf-8")
            lock = build_cohort_lock(
                repo,
                labels_path,
                manifest_path,
                ["sample"],
                self._model_config(),
            )
            self.assertEqual(verify_cohort_lock(lock), [])
            changed_label = self._label()
            changed_label["overall_note"] = "changed after freeze"
            labels_path.write_text(json.dumps({"samples": {"sample": changed_label}}), encoding="utf-8")
            drifted_gt = json.loads(json.dumps(lock))
            drifted_gt["labels"]["sha256"] = sha256_file(labels_path)
            drifted_gt["labels"]["size_bytes"] = labels_path.stat().st_size
            self.assertTrue(any("GT 已漂移" in error for error in verify_cohort_lock(drifted_gt)))
            invalid_label = self._label()
            invalid_label["partition"] = "calibration"
            labels_path.write_text(json.dumps({"samples": {"sample": invalid_label}}), encoding="utf-8")
            invalid_contract = json.loads(json.dumps(lock))
            invalid_contract["labels"]["sha256"] = sha256_file(labels_path)
            invalid_contract["labels"]["size_bytes"] = labels_path.stat().st_size
            self.assertTrue(any("GT partition" in error for error in verify_cohort_lock(invalid_contract)))
            labels_path.write_text(json.dumps({"samples": {"sample": self._label()}}), encoding="utf-8")
            changed_manifest = {
                "samples": [{
                    "id": "sample",
                    "group": "blind",
                    "product_category": "changed",
                    "target_market": "th",
                    "creator_video": str(creator),
                    "benchmark_video": str(benchmark),
                }]
            }
            manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
            drifted_manifest = json.loads(json.dumps(lock))
            drifted_manifest["manifest"]["sha256"] = sha256_file(manifest_path)
            drifted_manifest["manifest"]["size_bytes"] = manifest_path.stat().st_size
            self.assertTrue(any("product_category 已漂移" in error for error in verify_cohort_lock(drifted_manifest)))
            manifest_path.write_text(json.dumps({"samples": [{
                "id": "sample",
                "group": "blind",
                "product_category": "test",
                "target_market": "th",
                "creator_video": str(creator),
                "benchmark_video": str(benchmark),
            }]}), encoding="utf-8")
            drifted_code = json.loads(json.dumps(lock))
            drifted_code["code"]["worktree_fingerprint_sha256"] = "0" * 64
            self.assertTrue(any("工作树" in error for error in verify_cohort_lock(drifted_code)))
            spent = spend_cohort_lock(lock, "结果已打开")
            self.assertEqual(spent["status"], "spent")
            creator.write_bytes(b"changed")
            self.assertTrue(any("creator" in error for error in verify_cohort_lock(lock)))

    def test_lock_rejects_video_reused_by_another_blind_sample(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "shared.mp4"
            new_creator = root / "new-creator.mp4"
            benchmark = root / "benchmark.mp4"
            shared.write_bytes(b"shared")
            new_creator.write_bytes(b"new")
            benchmark.write_bytes(b"benchmark")
            self._use_validation_root(root)
            labels_path = root / "labels.json"
            manifest_path = root / "manifest.json"
            labels_path.write_text(json.dumps({"samples": {"new": self._label()}}), encoding="utf-8")
            manifest_path.write_text(json.dumps({"samples": [
                {
                    "id": "old-blind",
                    "group": "blind",
                    "creator_video": str(shared),
                    "benchmark_video": str(benchmark),
                },
                {
                    "id": "new",
                    "group": "blind",
                    "product_category": "test",
                    "target_market": "th",
                    "creator_video": str(new_creator),
                    "benchmark_video": str(shared),
                },
            ]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "复用了 old-blind.creator_video"):
                build_cohort_lock(
                    repo,
                    labels_path,
                    manifest_path,
                    ["new"],
                    self._model_config(),
                )


if __name__ == "__main__":
    unittest.main()
