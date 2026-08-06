from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.parse import normalize_video_fact_result
from flayr_core.llm.pipeline import _merge_video_fact_recovery
from flayr_core.postprocess.derive import _derive_one
from flayr_core.postprocess.repair_stages import align_clear_commerce_evidence, align_timed_cta_from_transcript
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_codes,
    stage_evidence_contract_issues,
    stage_evidence_recovery_targets,
    normalize_stage_evidence_checks,
    qualified_stage_evidence_ids,
    stage_evidence_contract,
)


class StageEvidenceContractTests(unittest.TestCase):
    @staticmethod
    def _analysis() -> dict[str, object]:
        return {"videos": {"benchmark": {}, "creator": {}}}

    @staticmethod
    def _checks(status: str = "unknown", strength: str | None = None) -> list[dict[str, object]]:
        return [
            {
                "stage": stage,
                "status": status,
                "coverage": "complete" if status in {"present", "absent"} else "unknown",
                "evidence_ids": ["C1"] if status == "present" else [],
                "observed_signals": list(stage_evidence_contract(stage).required_signals)
                if status == "present" else [],
                "missing_signals": [],
                "evidence_strength": strength,
            }
            for stage in stage_codes()
        ]

    def test_omitted_checks_are_unknown_and_recovery_is_bounded(self) -> None:
        checks = normalize_stage_evidence_checks([], {"C1"})
        self.assertEqual([item["status"] for item in checks], ["unknown"] * 6)
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "inferred"}],
        }
        self.assertEqual(stage_evidence_recovery_targets(side), list(stage_codes()))
        self.assertEqual(stage_evidence_contract_issues(side), [])

    def test_present_requires_real_id_and_explicit_strength(self) -> None:
        checks = self._checks("present", "inferred")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "inferred"}],
        }
        issues = stage_evidence_contract_issues(side)
        self.assertIn("S1:present_without_explicit_strength", issues)
        self.assertIn("S6:present_without_explicit_strength", issues)

    def test_present_qualification_uses_unit_strength_and_required_signals(self) -> None:
        checks = self._checks("present", "inferred")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
        }
        self.assertEqual({"C1"}, qualified_stage_evidence_ids(side, "S1"))
        side["evidence_units"][0]["evidence_strength"] = "inferred"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))
        checks[0]["evidence_strength"] = "direct"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))

    def test_unknown_or_conflict_cannot_carry_evidence_ids(self) -> None:
        checks = self._checks("unknown")
        checks[0]["evidence_ids"] = ["C1"]
        checks[1]["status"] = "conflict"
        checks[1]["evidence_ids"] = ["C1"]
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
        }
        issues = stage_evidence_contract_issues(side)
        self.assertIn("S1:unknown_with_evidence", issues)
        self.assertIn("S2:conflict_with_evidence", issues)
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))

    def test_absent_requires_complete_coverage(self) -> None:
        checks = self._checks("absent")
        checks[0]["coverage"] = "partial"
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1"}],
        }
        self.assertIn("S1:absence_without_complete_coverage", stage_evidence_contract_issues(side))

    def test_stage1_normalization_does_not_silently_drop_units(self) -> None:
        units = [
            {
                "id": f"C{index}",
                "time_range": f"{index}s - {index + 0.5}s",
                "information": f"观察 {index}",
            }
            for index in range(1, 11)
        ]
        normalized = normalize_video_fact_result(
            "creator",
            {"evidence_units": units},
            self._analysis(),
        )
        self.assertEqual(len(normalized["evidence_units"]), 10)
        self.assertFalse(normalized["evidence_budget_exceeded"])

    def test_invalid_ids_are_filtered_without_turning_absence_into_presence(self) -> None:
        checks = normalize_stage_evidence_checks(
            [
                {
                    "stage": "S4",
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": ["C1", "C9"],
                    "evidence_strength": "direct",
                }
            ],
            {"C1"},
        )
        s4 = next(item for item in checks if item["stage"] == "S4")
        self.assertEqual(s4["evidence_ids"], ["C1"])
        s1 = next(item for item in checks if item["stage"] == "S1")
        self.assertEqual(s1["status"], "unknown")

    def test_visual_required_stage_cannot_qualify_from_voiceover_only(self) -> None:
        checks = self._checks("present", "direct")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "voiceover": "直接口播"}],
        }
        issues = stage_evidence_contract_issues(side)
        self.assertIn("S3:required_visual_channel_missing", issues)
        self.assertIn("S4:required_visual_channel_missing", issues)

    def test_duplicate_stage_projection_is_conflict_and_recovery_target(self) -> None:
        checks = normalize_stage_evidence_checks(
            [
                {"stage": "S1", "status": "unknown", "coverage": "unknown"},
                {"stage": "S1", "status": "present", "coverage": "complete", "evidence_ids": ["C1"]},
            ],
            {"C1"},
        )
        s1 = next(item for item in checks if item["stage"] == "S1")
        self.assertEqual(s1["status"], "conflict")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
        }
        self.assertIn("S1", stage_evidence_recovery_targets(side))

    def test_duplicate_unit_ids_are_reported_in_final_contract(self) -> None:
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": self._checks("unknown"),
            "evidence_units": [
                {"id": "C1", "evidence_strength": "direct"},
                {"id": "C1", "evidence_strength": "direct"},
            ],
        }
        self.assertIn("duplicate_evidence_ids:C1", stage_evidence_contract_issues(side))

    def test_duplicate_normalized_ids_are_rejected(self) -> None:
        units = [
            {"id": "C1", "time_range": "0s - 1s", "information": "第一条"},
            {"id": "C1", "time_range": "1s - 2s", "information": "第二条"},
        ]
        with self.assertRaisesRegex(SystemExit, "evidence IDs must be unique"):
            normalize_video_fact_result(
                "creator",
                {"evidence_units": units},
                self._analysis(),
            )

    def test_empty_or_non_object_evidence_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no valid evidence units"):
            normalize_video_fact_result(
                "creator",
                {"evidence_units": [None, "not-an-object"]},
                self._analysis(),
            )

    def test_recovery_only_replaces_target_stage_and_keeps_old_observations(self) -> None:
        base = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [
                    {"id": "C1", "time_range": "0s - 1s", "information": "原始观察"},
                ],
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage_evidence_checks": self._checks("unknown"),
            },
            self._analysis(),
        )
        recovery = {
            "candidate_evidence_units": [
                {
                    "id": "C2",
                    "time_range": "2s - 3s",
                    "information": "恢复观察",
                    "visual_fact": "画面直接可见",
                    "evidence_strength": "direct",
                }
            ],
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": [
                {
                    "stage": "S1",
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": ["C2"],
                    "observed_signals": list(stage_evidence_contract("S1").required_signals),
                    "missing_signals": [],
                }
            ],
        }
        merged = _merge_video_fact_recovery(
            "creator",
            base,
            recovery,
            self._analysis(),
            ["S1"],
        )
        self.assertEqual({"C1", "C2"}, {unit["id"] for unit in merged["evidence_units"]})
        checks = {item["stage"]: item for item in merged["stage_evidence_checks"]}
        self.assertEqual(checks["S1"]["status"], "present")
        self.assertEqual(checks["S1"]["evidence_ids"], ["C2"])
        self.assertEqual(checks["S2"]["status"], "unknown")
        self.assertEqual(stage_evidence_contract_issues(merged), [])

    @staticmethod
    def _active_side(role_code: str, s6_status: str = "unknown") -> dict[str, object]:
        checks: list[dict[str, object]] = []
        for stage in stage_codes():
            if stage == "S6" and s6_status == "present":
                checks.append(
                    {
                        "stage": "S6",
                        "status": "present",
                        "coverage": "complete",
                        "evidence_ids": [f"{role_code}6"],
                        "observed_signals": list(stage_evidence_contract("S6").required_signals),
                        "missing_signals": [],
                    }
                )
            else:
                checks.append(
                    {
                        "stage": stage,
                        "status": "unknown",
                        "coverage": "unknown",
                        "evidence_ids": [],
                        "observed_signals": [],
                        "missing_signals": [],
                    }
                )
        return {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [
                {
                    "id": f"{role_code}6",
                    "evidence_strength": "direct",
                    "visual_fact": "结尾画面可见行动入口",
                }
            ],
        }

    def test_active_s6_ceiling_requires_qualified_stage1_evidence(self) -> None:
        stage = {
            "model_severity": "large",
            "creator_s6": {
                "exists": True,
                "direct_order_met": True,
                "action_path_clear": True,
                "evidence_ids": ["C6"],
            },
            "benchmark_s6": {
                "exists": False,
                "evidence_ids": ["B6"],
            },
        }
        facts = {
            "video_understanding": {
                "creator": self._active_side("C", "unknown"),
                "benchmark": self._active_side("B", "unknown"),
            }
        }
        blocked = _derive_one("S6", stage, facts=facts)
        self.assertEqual(blocked["severity"], "large")
        self.assertTrue(
            any(item.get("status") == "stage_evidence_unresolved" for item in blocked["constraint_evaluations"])
        )

        facts["video_understanding"]["creator"] = self._active_side("C", "present")
        facts["video_understanding"]["benchmark"] = self._active_side("B", "present")
        applied = _derive_one("S6", stage, facts=facts)
        self.assertEqual(applied["severity"], "small")
        self.assertTrue(any(item.get("rule") == "S6_creator_cta_ceiling" and item.get("status") == "triggered" for item in applied["constraint_evaluations"]))

    def test_active_repair_paths_do_not_append_post_lock_facts(self) -> None:
        result = {
            "stage_analysis": [{} for _ in range(6)],
            "video_understanding": {
                "benchmark": self._active_side("B", "unknown"),
                "creator": self._active_side("C", "unknown"),
            },
        }
        result["stage_analysis"][2]["benchmark_time_range"] = "1.0s - 2.0s"
        result["stage_analysis"][5]["benchmark_time_range"] = "5.0s - 6.0s"
        result["stage_analysis"][5]["creator_time_range"] = "5.0s - 6.0s"
        before = {
            role: [dict(unit) for unit in result["video_understanding"][role]["evidence_units"]]
            for role in ("benchmark", "creator")
        }
        align_clear_commerce_evidence(result)
        align_timed_cta_from_transcript(
            result,
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        for role in ("benchmark", "creator"):
            self.assertEqual(before[role], result["video_understanding"][role]["evidence_units"])


if __name__ == "__main__":
    unittest.main()
