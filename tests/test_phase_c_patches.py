from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm import pipeline
from flayr_core.llm.payload import build_stage_review_payload
from flayr_core.llm.parse import normalize_analysis_result


def _s6_patch_fields(*, creator_id: str = "C1", benchmark_id: str = "B1") -> dict[str, object]:
    return {
        "creator_evidence_ids": [creator_id],
        "benchmark_evidence_ids": [benchmark_id],
        "creator_s6": {"exists": True, "evidence_ids": [creator_id]},
        "benchmark_s6": {"exists": False, "evidence_ids": [benchmark_id]},
    }


def _complete_s6_flags(*, exists: bool, evidence_id: str) -> dict[str, object]:
    enabled = exists
    return {
        "exists": exists,
        "module_type": "A",
        "direct_order_met": enabled,
        "action_path_clear": enabled,
        "soft_purchase_invitation_met": False,
        "offer_or_incentive_clear": enabled,
        "price_anchor_met": False,
        "urgency_evidence_met": False,
        "gift_stack_met": False,
        "guarantee_clear_met": False,
        "urgency_met": False,
        "product_value_recalled": enabled,
        "module_fit_met": enabled,
        "ending_position_met": enabled,
        "depends_on_valid_s4": False,
        "compliance_risk": False,
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "cta_reason": "明确购买路径" if exists else "没有购买路径",
        "evidence_ids": [evidence_id],
        "proposition_ids": [],
    }


def _phase_c_absent_flag(code: str) -> dict[str, object]:
    """Minimal fact payloads for the shared empty-evidence policy tests."""
    if code == "S1":
        return {"exists": False, "evidence_ids": []}
    if code == "S2":
        return {"exists": False, "evidence_ids": []}
    if code == "S3":
        return {
            "exists": False,
            "usage_evidence_state": "none",
            "usage_process_visible": False,
            "real_usage_met": False,
            "core_selling_point_visible": False,
            "evidence_ids": [],
        }
    if code == "S4":
        return {
            "effect_type": "none",
            "effect_evidence_state": "none",
            "effect_visible": False,
            "evidence_ids": [],
        }
    if code == "S5":
        return {"exists": False, "trust_evidence_type": "none", "evidence_ids": []}
    if code == "S6":
        return {"exists": False, "evidence_ids": []}
    raise AssertionError(f"unsupported stage: {code}")


def _phase_c_fields(code: str, *, creator: dict[str, object], benchmark: dict[str, object]) -> dict[str, object]:
    flag_name = "hook" if code == "S1" else code.lower()
    return {
        "creator_evidence_ids": [],
        "benchmark_evidence_ids": [],
        f"creator_{flag_name}": creator,
        f"benchmark_{flag_name}": benchmark,
    }


def _full_phase_c_result(facts: dict[str, object]) -> dict[str, object]:
    def stage(index: int) -> dict[str, object]:
        return {
            "stage": f"S{index} stage",
            "time_range": "标杆 0.0s - 2.0s / 达人 0.0s - 2.0s",
            "benchmark_time_range": "0.0s - 2.0s",
            "creator_time_range": "0.0s - 2.0s",
            "core_question": "阶段问题",
            "creator_module_id": "unknown",
            "benchmark_module_id": "unknown",
            "module_fit": "fit",
            "module_fit_reason": "阶段适配。",
            "task_completion": "complete",
            "gap_type": "structural",
            "gap_summary": ["可比较差异"],
            "voice_performance": {},
            "benchmark_summary": "标杆有表现",
            "benchmark_key_message": "标杆消息",
            "benchmark_evidence_ids": ["B1"],
            "benchmark_visual_evidence": ["标杆画面"],
            "benchmark_support_status": "supported",
            "benchmark_quote": "现在下单",
            "benchmark_quote_zh": "现在下单",
            "creator_summary": "达人有表现",
            "creator_key_message": "达人消息",
            "creator_evidence_ids": ["C1"],
            "creator_visual_evidence": ["达人画面"],
            "creator_support_status": "supported",
            "creator_quote": "现在下单",
            "creator_quote_zh": "现在下单",
            "gap": "达人缺少部分表现，影响购买。",
            "evidence": ["C1", "B1"],
            "severity": "large" if index == 6 else "medium",
            "creator_execution": 1,
            "benchmark_execution": 1,
            "painpoint_relevance": "both",
        }

    return {
        "one_line_summary": "结论",
        "one_line_verdict": "结论",
        "executive_summary": "结论",
        "stage_analysis": [stage(index) for index in range(1, 7)],
        "improvements": [{
            "title": "改进",
            "target_stage": "S1",
            "gmv_impact": "提升",
            "gap_type": "structural",
            "time_range": "0.0s - 2.0s",
            "creator_time_range": "0.0s - 2.0s",
            "benchmark_time_range": "0.0s - 2.0s",
            "problem": "问题",
            "benchmark_reference": "参考",
            "action": "行动",
            "priority": 1,
        }],
        "video_understanding": facts,
    }


class PhaseCPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = {
            "creator": {"evidence_units": [{"id": "C1"}]},
            "benchmark": {"evidence_units": [{"id": "B1"}]},
        }
        self.current = {
            "stage_analysis": [
                {
                    "stage": "S6 CTA",
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                    "creator_multimodal": {"integrated_effect": "strong"},
                    "benchmark_multimodal": {"integrated_effect": "strong"},
                    "_postprocess_state": {"s6_hard_fact_checks": {"status": "consistent"}},
                }
            ],
            "improvements": [{"title": "已通过的原建议"}],
        }

    def _review(self, fields: dict[str, object]) -> dict[str, object]:
        return {
            "stage_patches": [{"stage": "S6", "fields": fields}],
            "review_notes": ["切片确认达人有明确购买路径。"],
        }

    def test_rejects_protected_stage_fields(self) -> None:
        fields = _s6_patch_fields()
        fields["severity"] = "small"
        with self.assertRaisesRegex(SystemExit, "protected fields: severity"):
            pipeline.apply_stage_review_updates(
                self.current,
                self._review(fields),
                {},
                "",
                self.facts,
                allowed_stage_codes=["S6"],
            )

    def test_rejects_unknown_evidence_id(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unknown creator evidence: C-UNKNOWN"):
            pipeline.apply_stage_review_updates(
                self.current,
                self._review(_s6_patch_fields(creator_id="C-UNKNOWN")),
                {},
                "",
                self.facts,
                allowed_stage_codes=["S6"],
            )

    def test_rejects_nested_evidence_from_another_stage(self) -> None:
        facts = {
            "creator": {
                "evidence_units": [
                    {"id": "C1", "time_range": "0.0s - 2.0s"},
                    {"id": "C6", "time_range": "20.0s - 22.0s"},
                ]
            },
            "benchmark": {
                "evidence_units": [
                    {"id": "B1", "time_range": "0.0s - 2.0s"},
                    {"id": "B6", "time_range": "20.0s - 22.0s"},
                ]
            },
        }
        current = {
            "stage_analysis": [{
                "stage": "S6 CTA",
                "creator_time_range": "0.0s - 2.0s",
                "benchmark_time_range": "0.0s - 2.0s",
            }]
        }
        fields = _s6_patch_fields()
        fields["creator_s6"] = {"exists": True, "evidence_ids": ["C6"]}
        fields["benchmark_s6"] = {"exists": False, "evidence_ids": ["B6"]}

        with self.assertRaisesRegex(SystemExit, "must be a subset of creator_evidence_ids"):
            pipeline.apply_stage_review_updates(
                current,
                self._review(fields),
                {},
                "",
                facts,
                allowed_stage_codes=["S6"],
            )

    def test_s4_absence_patch_allows_empty_role_evidence(self) -> None:
        facts = {
            "creator": {"evidence_units": []},
            "benchmark": {"evidence_units": [{"id": "B1", "time_range": "0.0s - 2.0s"}]},
        }
        current = {
            "stage_analysis": [{
                "stage": "S4 效果呈现",
                "creator_time_range": "0.0s - 2.0s",
                "benchmark_time_range": "0.0s - 2.0s",
            }]
        }
        fields = {
            "creator_evidence_ids": [],
            "benchmark_evidence_ids": ["B1"],
            "creator_s4": {
                "effect_type": "none",
                "effect_evidence_state": "none",
                "effect_visible": False,
                "evidence_ids": [],
            },
            "benchmark_s4": {
                "effect_type": "none",
                "effect_evidence_state": "none",
                "effect_visible": False,
                "evidence_ids": [],
            },
        }

        patches = pipeline._validate_stage_review_patches(
            {"stage_patches": [{"stage": "S4", "fields": fields}]},
            facts,
            ["S4"],
            current_result=current,
        )

        self.assertEqual(patches["S4"]["creator_evidence_ids"], [])

    def test_s4_non_absence_patch_still_requires_evidence(self) -> None:
        facts = {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}}
        fields = {
            "creator_evidence_ids": [],
            "benchmark_evidence_ids": [],
            "creator_s4": {
                "effect_type": "process_visualization",
                "effect_evidence_state": "verified",
                "effect_visible": True,
                "evidence_ids": [],
            },
            "benchmark_s4": {
                "effect_type": "none",
                "effect_evidence_state": "none",
                "effect_visible": False,
                "evidence_ids": [],
            },
        }

        with self.assertRaisesRegex(SystemExit, "requires non-empty creator_evidence_ids"):
            pipeline._validate_stage_review_patches(
                {"stage_patches": [{"stage": "S4", "fields": fields}]},
                facts,
                ["S4"],
            )

    def test_s4_non_absence_empty_benchmark_evidence_is_rejected_symmetrically(self) -> None:
        fields = _phase_c_fields(
            "S4",
            creator=_phase_c_absent_flag("S4"),
            benchmark={
                "effect_type": "aesthetic_display",
                "effect_evidence_state": "result_only",
                "effect_visible": True,
                "evidence_ids": [],
            },
        )

        with self.assertRaisesRegex(SystemExit, "requires non-empty benchmark_evidence_ids"):
            pipeline._validate_stage_review_patches(
                {"stage_patches": [{"stage": "S4", "fields": fields}]},
                {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
                ["S4"],
            )

    def test_s4_non_absence_empty_nested_evidence_is_rejected_when_stage_evidence_exists(self) -> None:
        fields = {
            "creator_evidence_ids": ["C1"],
            "benchmark_evidence_ids": [],
            "creator_s4": {
                "effect_type": "process_visualization",
                "effect_evidence_state": "verified",
                "effect_visible": True,
                "evidence_ids": [],
            },
            "benchmark_s4": _phase_c_absent_flag("S4"),
        }

        with self.assertRaisesRegex(SystemExit, "requires non-empty creator_s4.evidence_ids"):
            pipeline._validate_stage_review_patches(
                {"stage_patches": [{"stage": "S4", "fields": fields}]},
                {"creator": {"evidence_units": [{"id": "C1"}]}, "benchmark": {"evidence_units": []}},
                ["S4"],
            )

    def test_s3_none_empty_evidence_is_allowed_but_partial_empty_evidence_is_rejected(self) -> None:
        absent_fields = _phase_c_fields(
            "S3",
            creator=_phase_c_absent_flag("S3"),
            benchmark=_phase_c_absent_flag("S3"),
        )
        patches = pipeline._validate_stage_review_patches(
            {"stage_patches": [{"stage": "S3", "fields": absent_fields}]},
            {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
            ["S3"],
        )
        self.assertEqual(patches["S3"]["creator_evidence_ids"], [])

        partial_creator = dict(_phase_c_absent_flag("S3"))
        partial_creator.update(
            {
                "exists": True,
                "usage_evidence_state": "partial",
                "usage_process_visible": True,
                "real_usage_met": True,
            }
        )
        with self.assertRaisesRegex(SystemExit, "requires non-empty creator_evidence_ids"):
            pipeline._validate_stage_review_patches(
                {
                    "stage_patches": [{
                        "stage": "S3",
                        "fields": _phase_c_fields(
                            "S3",
                            creator=partial_creator,
                            benchmark=_phase_c_absent_flag("S3"),
                        ),
                    }]
                },
                {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
                ["S3"],
            )

    def test_absent_empty_evidence_policy_covers_s1_s5_s6_and_keeps_s2_strict(self) -> None:
        for code in ("S1", "S5", "S6"):
            with self.subTest(stage=code):
                fields = _phase_c_fields(
                    code,
                    creator=_phase_c_absent_flag(code),
                    benchmark=_phase_c_absent_flag(code),
                )
                patches = pipeline._validate_stage_review_patches(
                    {"stage_patches": [{"stage": code, "fields": fields}]},
                    {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
                    [code],
                )
                self.assertEqual(patches[code]["creator_evidence_ids"], [])

        with self.assertRaisesRegex(SystemExit, "requires non-empty creator_evidence_ids"):
            pipeline._validate_stage_review_patches(
                {
                    "stage_patches": [{
                        "stage": "S2",
                        "fields": _phase_c_fields(
                            "S2",
                            creator=_phase_c_absent_flag("S2"),
                            benchmark=_phase_c_absent_flag("S2"),
                        ),
                    }]
                },
                {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
                ["S2"],
            )

    def test_legal_patch_runs_repair_validation_and_resolver_after_stale_state_is_removed(self) -> None:
        facts = {
            "creator": {"evidence_units": [{"id": "C1", "time_range": "0.0s - 2.0s", "voiceover": "现在下单", "functions": ["S1", "S2", "S3", "S4", "S5", "S6"]}]},
            "benchmark": {"evidence_units": [{"id": "B1", "time_range": "0.0s - 2.0s", "voiceover": "现在下单", "functions": ["S1", "S2", "S3", "S4", "S5", "S6"]}]},
        }
        current = _full_phase_c_result(facts)
        current["stage_analysis"][5].update({
            "creator_multimodal": {"integrated_effect": "strong"},
            "benchmark_multimodal": {"integrated_effect": "strong"},
            "_postprocess_state": {"s6_hard_fact_checks": {"status": "consistent"}},
        })
        fields = _s6_patch_fields()
        fields["creator_s6"] = _complete_s6_flags(exists=True, evidence_id="C1")
        fields["benchmark_s6"] = _complete_s6_flags(exists=False, evidence_id="B1")
        # This fixture is intentionally narrow to the patch contract. The test
        # verifies that the production validator is reached; report narration
        # completeness is covered by its own dedicated contract tests.
        with mock.patch.object(
            pipeline,
            "validate_evidence_alignment",
            wraps=pipeline.validate_evidence_alignment,
        ) as evidence_validation, mock.patch.object(pipeline, "validate_quality_contract") as quality_validation:
            refined = pipeline.apply_stage_review_updates(
                current,
                self._review(fields),
                {},
                "",
                facts,
                allowed_stage_codes=["S6"],
            )

        evidence_validation.assert_called_once()
        quality_validation.assert_called_once()
        stage = refined["stage_analysis"][5]
        self.assertIsNone(stage["creator_multimodal"])
        self.assertIsNone(stage["benchmark_multimodal"])
        self.assertNotIn("_postprocess_state", stage)
        self.assertEqual(stage["creator_s6"]["evidence_ids"], ["C1"])
        self.assertEqual(stage["severity"], "small")
        self.assertEqual(stage["severity_derivation"]["status"], "constrained")

    def test_patch_snapshot_is_not_a_whole_stage_snapshot(self) -> None:
        before = {
            "stage_analysis": [{"stage": "S6 CTA", "severity": "large", "model_severity": "large", **_s6_patch_fields()}]
        }
        after = {
            "stage_analysis": [{"stage": "S6 CTA", "severity": "small", "model_severity": "large", **_s6_patch_fields()}]
        }
        snapshots = pipeline._phase_c_patch_snapshots(before, after, self._review(_s6_patch_fields()))
        self.assertEqual(snapshots[0]["stage"], "S6")
        self.assertEqual(snapshots[0]["before"]["resolution"]["severity"], "large")
        self.assertEqual(snapshots[0]["after"]["resolution"]["severity"], "small")
        self.assertNotIn("gap", snapshots[0]["before"]["patchable_fields"])
        self.assertNotIn("before_stage_analysis", snapshots[0])

    def test_refinalization_preserves_the_original_model_severity(self) -> None:
        normalized = normalize_analysis_result(
            {
                "stage_analysis": [
                    {"stage": f"S{index} stage", "severity": "small", "model_severity": "large"}
                    for index in range(1, 7)
                ],
                "improvements": [{"title": "已通过的原建议"}],
            }
        )
        self.assertEqual(normalized["stage_analysis"][0]["severity"], "small")
        self.assertEqual(normalized["stage_analysis"][0]["model_severity"], "large")

    def test_payload_requests_patch_contract_not_whole_stage_updates(self) -> None:
        payload = build_stage_review_payload(
            "test",
            {"videos": {}},
            self.facts,
            {"stage_analysis": [{"stage": "S6 CTA", "creator_time_range": "0s - 2s", "benchmark_time_range": "0s - 2s"}]},
            ["S6"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn('"stage_patches"', text)
        self.assertNotIn('"stage_updates"', text)
        self.assertIn("不得输出或修改 severity", text)

    def test_s4_review_payload_requires_effect_evidence_state(self) -> None:
        payload = build_stage_review_payload(
            "test",
            {"videos": {}},
            self.facts,
            {
                "stage_analysis": [
                    {
                        "stage": "S4 效果呈现",
                        "creator_time_range": "0s - 2s",
                        "benchmark_time_range": "0s - 2s",
                    }
                ]
            },
            ["S4"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn('"effect_evidence_state": "none|result_only|verified|uncertain"', text)
        self.assertIn("两侧都必须输出 effect_evidence_state", text)
