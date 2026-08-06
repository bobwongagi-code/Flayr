from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.parse import normalize_video_fact_result
from flayr_core.llm.pipeline import _merge_video_fact_recovery, detect_low_confidence_stages
from flayr_core.llm.payload import _compact_comparison_facts
from flayr_core.postprocess.derive import _derive_one
from flayr_core.postprocess.claims_my import reconcile_certification_ownership
from flayr_core.postprocess.repair_evidence import (
    ground_improvement_evidence,
    ground_stage_visual_evidence,
    reconcile_s5_trust_sources,
    reconcile_unsupported_cta,
)
from flayr_core.postprocess.repair_claims import derive_product_visibility
from flayr_core.postprocess.claims_my import discard_unreferenced_certification_claims
from flayr_core.postprocess.repair_stages import (
    align_clear_commerce_evidence,
    align_timed_cta_from_transcript,
    apply_comparison_eligibility,
)
from flayr_core.postprocess.global_diagnosis import _attention_side_status, _dominant_selling_point
from flayr_core.postprocess.validate import validate_stage_evidence_qualification
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_codes,
    stage_evidence_contract_issues,
    stage_evidence_readiness,
    stage_evidence_recovery_targets,
    normalize_stage_evidence_checks,
    qualified_stage_evidence_ids,
    qualified_stage_evidence_units,
    stage_analysis_evidence_view,
    stage_analysis_stage_context,
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

    def test_budget_flag_opens_recovery_for_every_registered_stage(self) -> None:
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": self._checks("present", "direct"),
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
            "evidence_budget_exceeded": True,
        }
        self.assertEqual(stage_evidence_recovery_targets(side), list(stage_codes()))
        self.assertEqual(stage_evidence_recovery_targets(side, include_budget=False), [])

    def test_budget_flag_is_not_qualified_until_pipeline_recovery_finishes(self) -> None:
        checks = self._checks("present", "direct")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
            "evidence_budget_exceeded": True,
        }
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")
        self.assertEqual(qualified_stage_evidence_ids(side, "S1"), set())
        side["stage1_recovery"] = {
            "source": "pipeline",
            "status": "applied",
            "unresolved_stages": [],
        }
        self.assertEqual(stage_evidence_readiness(side, "S1"), "present")
        self.assertEqual(qualified_stage_evidence_ids(side, "S1"), {"C1"})
        side["stage1_recovery"] = {
            "source": "pipeline",
            "status": "applied_with_unresolved",
            "unresolved_stages": ["S1"],
        }
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")

    def test_active_comparison_is_blocked_until_budget_recovery_is_trusted(self) -> None:
        sides = {}
        for role_code in ("C", "B"):
            checks = self._checks("present", "direct")
            unit_id = "C1" if role_code == "C" else "B1"
            for check in checks:
                check["evidence_ids"] = [unit_id]
            sides["creator" if role_code == "C" else "benchmark"] = {
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage_evidence_checks": checks,
                "evidence_units": [{
                    "id": unit_id,
                    "evidence_strength": "direct",
                    "visual_fact": "画面事实",
                }],
                "evidence_budget_exceeded": True,
            }
        result = {
            "video_understanding": sides,
            "comparison_contract": {
                "overall_status": "full_direct",
                "stage_eligibility": {
                    stage: {"status": "direct", "basis": "同款"}
                    for stage in stage_codes()
                },
            },
            "stage_analysis": [{"stage": stage} for stage in stage_codes()],
        }
        apply_comparison_eligibility(result)
        self.assertTrue(
            all(stage.get("comparison_status") == "not_directly_comparable" for stage in result["stage_analysis"])
        )

    def test_analysis_view_exposes_only_qualified_units_for_all_stages(self) -> None:
        side = self._active_side("C", "present")
        side["evidence_units"].append({
            "id": "C_UNQUALIFIED",
            "evidence_strength": "direct",
            "visual_fact": "不能进入分析视图",
        })
        side["attention_scan_audit"] = {"evidence_ids": ["C6", "C_UNQUALIFIED"]}
        side["content_summary"] = "不应绕过资格闸门进入分析"
        side["structure_event_checks"] = [{"evidence_ids": ["C_UNQUALIFIED"]}]
        view = stage_analysis_evidence_view({"creator": side, "benchmark": side})
        self.assertEqual({"C6"}, {unit["id"] for unit in view["creator"]["evidence_units"]})
        self.assertNotIn("attention_scan_audit", view["creator"])
        self.assertNotIn("content_summary", view["creator"])
        self.assertNotIn("structure_event_checks", view["creator"])
        self.assertEqual(view["creator"]["analysis_evidence_scope"], "qualified_stage_evidence_only")

        side["evidence_budget_exceeded"] = True
        blocked = stage_analysis_evidence_view({"creator": side})
        self.assertEqual(blocked["creator"]["evidence_units"], [])
        s6_check = next(item for item in blocked["creator"]["stage_evidence_checks"] if item["stage"] == "S6")
        self.assertEqual(s6_check["status"], "unknown")
        self.assertEqual(blocked["creator"]["stage_evidence_readiness"]["S6"], "unknown")

    def test_analysis_view_partitions_full_units_by_qualified_stage(self) -> None:
        side = self._active_side("C", "present")
        checks = self._checks("unknown")
        checks[0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
        }
        checks[5] = {
            "stage": "S6",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C6"],
            "observed_signals": list(stage_evidence_contract("S6").required_signals),
            "missing_signals": [],
        }
        side["stage_evidence_checks"] = checks
        side["evidence_units"] = [
            {
                "id": "C1",
                "time_range": "0s - 1s",
                "evidence_strength": "direct",
                "visual_fact": "S1 画面",
            },
            {
                "id": "C6",
                "time_range": "5s - 6s",
                "evidence_strength": "direct",
                "visual_fact": "S6 画面",
            },
        ]
        view = stage_analysis_evidence_view({"creator": side})["creator"]
        self.assertEqual([unit["id"] for unit in view["stage_evidence_units"]["S1"]], ["C1"])
        self.assertEqual([unit["id"] for unit in view["stage_evidence_units"]["S6"]], ["C6"])
        self.assertEqual(view["evidence_units"][0]["qualified_stages"], ["S1"])
        self.assertEqual(view["evidence_units"][1]["qualified_stages"], ["S6"])
        self.assertEqual(view["stage_evidence_units"]["S1"][0]["visual_fact"], "S1 画面")

    def test_derived_visibility_does_not_consume_unqualified_active_units(self) -> None:
        side = self._active_side("C", "unknown")
        side["evidence_units"][0].update({
            "time_range": "0s - 2s",
            "product_visible": True,
            "product_coverage": "high",
        })
        result = {
            "video_understanding": {"creator": side},
            "product_visibility": {"estimation_note": "保留模型估计"},
        }
        analysis = {"videos": {"creator": {"duration_seconds": 10.0}}}
        self.assertEqual(qualified_stage_evidence_units(side), [])
        derive_product_visibility(result, analysis)
        self.assertEqual(result["product_visibility"]["estimation_note"], "保留模型估计")

    def test_missing_recovery_target_replaces_old_qualification_with_unknown(self) -> None:
        base = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [
                    {
                        "id": "C1",
                        "time_range": "0s - 1s",
                        "information": "原始观察",
                        "visual_fact": "画面",
                        "evidence_strength": "direct",
                    },
                ],
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage_evidence_checks": self._checks("present", "direct"),
            },
            self._analysis(),
        )
        recovery = {
            "candidate_evidence_units": [],
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": [
                {
                    "stage": "S1",
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": ["C1"],
                    "observed_signals": list(stage_evidence_contract("S1").required_signals),
                    "missing_signals": [],
                }
            ],
        }
        merged = _merge_video_fact_recovery("creator", base, recovery, self._analysis(), ["S1", "S2"])
        checks = {item["stage"]: item for item in merged["stage_evidence_checks"]}
        self.assertEqual(checks["S1"]["status"], "present")
        self.assertEqual(checks["S2"]["status"], "unknown")
        self.assertEqual(qualified_stage_evidence_ids(merged, "S2"), set())
        self.assertEqual(stage_evidence_contract_issues(merged), [])

    def test_comparison_compact_view_does_not_reintroduce_raw_units(self) -> None:
        side = self._active_side("C", "present")
        side["evidence_units"].append({
            "id": "C_UNQUALIFIED",
            "information": "不应进入比较输入",
            "visual_fact": "不应进入比较输入",
            "evidence_strength": "direct",
        })
        compact = _compact_comparison_facts(side)
        self.assertEqual([unit["id"] for unit in compact["evidence_units"]], ["C6"])
        self.assertEqual([unit["id"] for unit in compact["stage_evidence_units"]["S6"]], ["C6"])

    def test_comparison_compact_qualifies_visual_stage_from_authoritative_units(self) -> None:
        checks = self._checks("unknown")
        checks[3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C4"],
            "observed_signals": list(stage_evidence_contract("S4").required_signals),
            "missing_signals": [],
        }
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{
                "id": "C4",
                "time_range": "3s - 4s",
                "evidence_strength": "direct",
                "visual_fact": "结果前后状态可见",
            }],
        }
        compact = _compact_comparison_facts(side)
        self.assertEqual(compact["qualified_stage_evidence_ids"]["S4"], ["C4"])
        self.assertEqual([unit["id"] for unit in compact["stage_evidence_units"]["S4"]], ["C4"])

    def test_global_selling_point_ignores_unqualified_observation(self) -> None:
        side = {
            "selling_point_observations": [
                {
                    "candidate_id": "unqualified",
                    "visual_share": 1.0,
                    "speech_share": 1.0,
                    "evidence_ids": ["C_UNQUALIFIED"],
                },
                {
                    "candidate_id": "qualified",
                    "visual_share": 0.2,
                    "speech_share": 0.3,
                    "evidence_ids": ["C2"],
                },
            ]
        }
        self.assertEqual(
            _dominant_selling_point(side, {"C2"})["candidate_id"],
            "qualified",
        )

    def test_global_attention_does_not_treat_unqualified_observation_as_clean_or_dirty(self) -> None:
        side = self._active_side("C", "unknown")
        side["gate_observation_status"] = {"attention_scan": "complete"}
        side["attention_competitors"] = [{
            "evidence_ids": ["C_UNQUALIFIED"],
            "participates_in_product_task": False,
            "high_salience": True,
            "persistent_motion": True,
            "occludes_proof_area": True,
            "time_ranges": ["1s - 5s"],
        }]
        self.assertEqual(_attention_side_status(side, "full_temporal"), ("unknown", [], 0.0))

    def test_global_attention_does_not_treat_excluded_observation_as_clean_when_other_evidence_is_qualified(self) -> None:
        side = self._active_side("C", "present")
        side["gate_observation_status"] = {"attention_scan": "complete"}
        side["attention_competitors"] = [{
            "evidence_ids": ["C_UNQUALIFIED"],
            "participates_in_product_task": False,
            "high_salience": True,
            "persistent_motion": True,
            "occludes_proof_area": True,
            "time_ranges": ["1s - 5s"],
        }]
        self.assertEqual(_attention_side_status(side, "full_temporal"), ("unknown", [], 0.0))

    def test_global_variant_focus_does_not_treat_excluded_variant_as_single_focus(self) -> None:
        side = self._active_side("C", "present")
        checks = side["stage_evidence_checks"]
        for check in checks:
            if check["stage"] == "S2":
                check.update({
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": ["C2"],
                    "observed_signals": list(stage_evidence_contract("S2").required_signals),
                    "missing_signals": [],
                })
            elif check["stage"] in {"S3", "S4"}:
                check.update({
                    "status": "absent",
                    "coverage": "complete",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": list(stage_evidence_contract(check["stage"]).required_signals),
                })
        side["evidence_units"] = [
            {
                "id": "C2",
                "evidence_strength": "direct",
                "visual_fact": "产品身份可见",
                "variant_ids": ["variant_a"],
            },
            {
                "id": "C_UNQUALIFIED",
                "evidence_strength": "direct",
                "visual_fact": "另一个变体出现在未资格化观察中",
                "variant_ids": ["variant_a", "variant_b"],
            },
        ]
        side["gate_observation_status"] = {"variant_focus": "complete"}
        from flayr_core.postprocess.global_diagnosis import _variant_side_status

        self.assertEqual(_variant_side_status(side, "full_temporal")[0], "unknown")

    def test_analysis_view_does_not_preserve_invalid_absent_as_certain_missing(self) -> None:
        checks = self._checks("absent")
        checks[0]["coverage"] = "partial"
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [],
        }
        view = stage_analysis_evidence_view(side)
        s1 = next(item for item in view["stage_evidence_checks"] if item["stage"] == "S1")
        self.assertEqual(s1["status"], "unknown")
        self.assertEqual(view["stage_evidence_readiness"]["S1"], "unknown")

    def test_analysis_view_hides_unqualified_signal_claims(self) -> None:
        checks = self._checks("unknown")
        checks[2] = {
            "stage": "S3",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S3").required_signals),
            "missing_signals": [],
        }
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "inferred", "visual_fact": "未经资格化"}],
        }
        view = stage_analysis_evidence_view(side)
        s3 = next(item for item in view["stage_evidence_checks"] if item["stage"] == "S3")
        self.assertEqual(s3["status"], "unknown")
        self.assertEqual(s3["observed_signals"], [])
        self.assertEqual(s3["missing_signals"], [])
        self.assertIsNone(s3["evidence_strength"])

    def test_phase_c_low_confidence_detector_ignores_unqualified_units(self) -> None:
        side = self._active_side("C", "unknown")
        side["evidence_units"].append(
            {
                "id": "C_UNQUALIFIED",
                "evidence_strength": "direct",
                "visual_fact": "证据不足，需要人工复核",
            }
        )
        result = {
            "video_understanding": {"creator": side},
            "stage_analysis": [
                {
                    "stage": "S4",
                    "severity": "medium",
                    "creator_evidence_ids": ["C_UNQUALIFIED"],
                    "creator_visual_evidence": [],
                }
            ],
        }
        self.assertEqual(detect_low_confidence_stages(result), [])

    def test_unknown_stage_does_not_delete_certification_claim_as_absent(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "stage_evidence_checks": self._checks("unknown"),
                    "evidence_units": [],
                },
                "benchmark": {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "stage_evidence_checks": self._checks("unknown"),
                    "evidence_units": [],
                },
            },
            "stage_analysis": [{
                "stage": f"S{index}",
                "creator_evidence_ids": [],
                "benchmark_evidence_ids": [],
                "creator_summary": "视频提到认证材料，但尚未完成该阶段证据采集。",
                "benchmark_summary": "视频提到认证材料，但尚未完成该阶段证据采集。",
            } for index in range(1, 7)],
        }
        discard_unreferenced_certification_claims(result)
        self.assertIn("认证", result["stage_analysis"][4]["creator_summary"])
        self.assertIn("认证", result["stage_analysis"][4]["benchmark_summary"])

    def test_phase_c_stage_context_drops_stale_stage_claims_and_unqualified_ids(self) -> None:
        stage = {
            "stage": "S6 CTA",
            "creator_time_range": "5s - 6s",
            "benchmark_time_range": "5s - 6s",
            "core_question": "能否推动行动",
            "creator_evidence_ids": ["C6", "C_UNQUALIFIED"],
            "creator_summary": "旧结论不应进入复核输入",
        }
        facts = {
            "creator": self._active_side("C", "present"),
            "benchmark": self._active_side("B", "present"),
        }
        context = stage_analysis_stage_context(stage, facts, "S6")
        self.assertEqual(context["creator_evidence_ids"], ["C6"])
        self.assertEqual(context["benchmark_evidence_ids"], ["B6"])
        self.assertNotIn("creator_summary", context)
        self.assertEqual(context["creator_stage_evidence_readiness"], "present")

    def test_stage_evidence_validator_rejects_forged_present_after_budget_cut(self) -> None:
        sides = {}
        for role_code in ("C", "B"):
            unit_id = "C1" if role_code == "C" else "B1"
            checks = self._checks("present", "direct")
            for check in checks:
                check["evidence_ids"] = [unit_id]
            sides["creator" if role_code == "C" else "benchmark"] = {
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage_evidence_checks": checks,
                "evidence_units": [{"id": unit_id, "evidence_strength": "direct", "visual_fact": "画面事实"}],
                "evidence_budget_exceeded": True,
            }
        result = {
            "video_understanding": sides,
            "stage_analysis": [
                {
                    "stage": stage,
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                }
                for stage in stage_codes()
            ],
        }
        with self.assertRaisesRegex(SystemExit, "阶段证据仍为 unknown"):
            validate_stage_evidence_qualification(result)

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

    def test_model_cannot_claim_pipeline_recovery_status(self) -> None:
        normalized = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [{"id": "C1", "information": "观察"}],
                "stage1_recovery": {"source": "pipeline", "status": "applied"},
            },
            self._analysis(),
        )
        self.assertEqual(normalized["stage1_recovery"], {})

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
        self.assertEqual(blocked["execution_observation"]["status"], "stage_evidence_unresolved")
        self.assertIsNone(blocked["execution_observation"]["creator"])
        self.assertIsNone(blocked["execution_observation"]["benchmark"])

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

    def test_unknown_s6_evidence_does_not_rewrite_cta_semantics_or_add_placeholder(self) -> None:
        result = {
            "stage_analysis": [{} for _ in range(6)],
            "video_understanding": {
                "benchmark": self._active_side("B", "unknown"),
                "creator": self._active_side("C", "unknown"),
            },
        }
        result["stage_analysis"][5] = {
            "stage": "S6",
            "creator_s6": {
                "exists": True,
                "direct_order_met": False,
                "action_path_clear": False,
                "evidence_ids": ["C6"],
                "cta_reason": "模型认为有 CTA",
            },
            "creator_quote": "请关注",
            "creator_evidence_ids": ["C6"],
        }
        reconcile_unsupported_cta(result)
        flag = result["stage_analysis"][5]["creator_s6"]
        self.assertTrue(flag["exists"])
        self.assertNotIn("C_NO_CTA", {unit["id"] for unit in result["video_understanding"]["creator"]["evidence_units"]})
        self.assertEqual(result["stage_analysis"][5]["creator_evidence_ids"], [])

    def test_active_s5_reconciliation_never_uses_unqualified_source_ids(self) -> None:
        side = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        checks[4] = {
            "stage": "S5",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C5"],
            "observed_signals": list(stage_evidence_contract("S5").required_signals),
            "missing_signals": [],
            "evidence_strength": "direct",
        }
        side["stage_evidence_checks"] = checks
        side["evidence_units"] = [
            {
                "id": "C5",
                "evidence_strength": "direct",
                "trust_source_status": "explicit_present",
                "trust_source_signals": ["authority"],
                "trust_source_reference": "机构报告",
                "visual_fact": "画面可见机构报告",
            },
            {
                "id": "C6",
                "evidence_strength": "direct",
                "trust_source_status": "explicit_present",
                "trust_source_signals": ["authority"],
                "trust_source_reference": "不应被 S5 消费",
                "visual_fact": "画面可见来源",
            },
        ]
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {"creator": side, "benchmark": side.copy()},
            "improvements": [],
        }
        stage = result["stage_analysis"][4]
        stage["creator_s5"] = {
            "exists": True,
            "independent_trust_purpose": True,
            "duplicates_other_stage": False,
            "trust_claim_specific": True,
            "product_relevance_met": True,
            "trust_basis": "authority",
            "trust_evidence_type": "hard",
            "evidence_ids": ["C6", "C5"],
            "trust_source_evidence_ids": ["C6", "C5"],
        }
        stage["benchmark_s5"] = dict(stage["creator_s5"])
        reconcile_s5_trust_sources(result, True)
        self.assertEqual(stage["creator_s5"]["trust_source_evidence_ids"], ["C5"])

    def test_active_certification_path_does_not_create_stage1_evidence(self) -> None:
        side = self._active_side("C", "unknown")
        result = {
            "stage_analysis": [{"stage": f"S{index}", "creator_quote": ""} for index in range(1, 7)],
            "video_understanding": {"creator": side, "benchmark": self._active_side("B", "unknown")},
        }
        result["stage_analysis"][1]["creator_quote"] = "已通过 KKM 认证"
        before = [dict(unit) for unit in side["evidence_units"]]
        reconcile_certification_ownership(result)
        self.assertEqual(before, side["evidence_units"])
        self.assertNotIn("C_CERT_S5", {unit["id"] for unit in side["evidence_units"]})

    def test_active_certification_cleanup_does_not_consume_unqualified_cert_unit(self) -> None:
        side = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        checks[4] = {
            "stage": "S5",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C5"],
            "observed_signals": list(stage_evidence_contract("S5").required_signals),
            "missing_signals": [],
            "evidence_strength": "direct",
        }
        side["stage_evidence_checks"] = checks
        side["evidence_units"] = [
            {
                "id": "C5",
                "evidence_strength": "direct",
                "information": "明确展示机构认证",
                "voiceover": "已通过 KKM 认证",
            },
            {
                "id": "C_UNQUALIFIED",
                "evidence_strength": "direct",
                "information": "未完成阶段资格化的认证观察",
                "voiceover": "已通过 KKM 认证",
            },
        ]
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": side,
                "benchmark": self._active_side("B", "unknown"),
            },
        }
        result["stage_analysis"][1]["creator_evidence_ids"] = ["C_UNQUALIFIED"]
        reconcile_certification_ownership(result)
        self.assertEqual(
            result["stage_analysis"][1]["creator_evidence_ids"],
            ["C_UNQUALIFIED"],
        )

    def test_nested_stage_references_are_closed_world_under_active_contract(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "unknown"),
                "benchmark": self._active_side("B", "unknown"),
            },
        }
        result["stage_analysis"][5]["creator_s6"] = {"evidence_ids": ["C6"]}
        with self.assertRaisesRegex(SystemExit, "嵌套阶段证据资格"):
            validate_stage_evidence_qualification(result)

    def test_active_visual_grounding_ignores_unqualified_stage_references(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "benchmark": self._active_side("B", "unknown"),
                "creator": self._active_side("C", "unknown"),
            },
        }
        stage = result["stage_analysis"][5]
        stage["creator_evidence_ids"] = ["C6"]
        stage["creator_visual_evidence"] = ["待复核：阶段资格未知"]
        ground_stage_visual_evidence(result)
        self.assertEqual(stage["creator_visual_evidence"], ["待复核：阶段资格未知"])

    def test_active_visual_grounding_keeps_qualified_stage_references(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "benchmark": self._active_side("B", "present"),
                "creator": self._active_side("C", "present"),
            },
        }
        stage = result["stage_analysis"][5]
        stage["creator_evidence_ids"] = ["C6"]
        ground_stage_visual_evidence(result)
        self.assertEqual(stage["creator_visual_evidence"], ["结尾画面可见行动入口"])

    def test_active_improvement_grounding_does_not_use_unqualified_units(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "benchmark": self._active_side("B", "unknown"),
                "creator": self._active_side("C", "unknown"),
            },
            "improvements": [
                {
                    "target_stage": "S6",
                    "title": "补充 CTA",
                    "suggestion": "在结尾补充产品购买入口",
                    "best_base_frame_time": "5.5s",
                }
            ],
        }
        result["stage_analysis"][5]["benchmark_evidence_ids"] = ["B6"]
        ground_improvement_evidence(result)
        item = result["improvements"][0]
        self.assertEqual(item["benchmark_evidence_ids"], [])
        self.assertEqual(item["base_frame_evidence_id"], "")
        self.assertEqual(item["base_frame_suitability"], "no_suitable_frame")


if __name__ == "__main__":
    unittest.main()
