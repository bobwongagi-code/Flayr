from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.artifacts import parse_time_range_seconds
from flayr_core.llm.parse import (
    normalize_analysis_result,
    normalize_video_fact_result,
    normalize_video_understanding,
)
from flayr_core.llm.pipeline import (
    _visual_input_timestamps,
    _mark_video_fact_coverage_audit_failed,
    _mark_stage1_qualification_recovered,
    _merge_video_fact_coverage_audit,
    _merge_video_fact_recovery,
    _materialize_stage_recovery_audit,
    _maybe_recover_video_facts,
    _normalize_segmented_stage,
    _reproject_segmented_stage_results,
    _authoritative_segmented_comparison_contract,
    _build_stage1_to_stage2_handoff,
    _stage1_to_stage2_handoff_issues,
    _video_fact_cache_stage1_coverage_issues,
    _run_stage1_qualification,
    detect_low_confidence_stages,
)
from flayr_core.llm.payload import (
    _compact_comparison_facts,
    _recovery_stage_windows,
    _replace_recovery_full_media,
    build_s1_boundary_hint_block,
)
from flayr_core.postprocess.derive import _derive_one, derive_severity_from_facts
from flayr_core.postprocess.claims_my import reconcile_certification_ownership
from flayr_core.postprocess.repair_evidence import (
    align_stage_flag_evidence,
    bind_timed_transcript_quotes,
    ground_improvement_evidence,
    ground_stage_visual_evidence,
    reconcile_s5_trust_sources,
    reconcile_unsupported_cta,
)
from flayr_core.postprocess.repair_claims import derive_product_visibility
from flayr_core.postprocess.repair_claims import clamp_result_time_ranges
from flayr_core.postprocess.claims_my import discard_unreferenced_certification_claims
from flayr_core.postprocess.repair_stages import (
    align_clear_commerce_evidence,
    align_timed_cta_from_transcript,
    apply_comparison_eligibility,
)
from flayr_core.postprocess.global_diagnosis import _attention_side_status, _dominant_selling_point
from flayr_core.report import stage_report_severity, stage_skipped
from flayr_core.postprocess.validate import (
    validate_evidence_alignment,
    validate_s2_contract_flags,
    validate_stage_evidence_qualification,
)
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE_EVIDENCE_SNAPSHOT_VERSION,
    STAGE1_PROJECTION_VERSION,
    STAGE1_ACQUISITION_VERSION,
    STAGE1_COVERAGE_AUDIT_VERSION,
    STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
    STAGE1_QUALIFICATION_GROUPS,
    STAGE_BOUNDARY_TESTS,
    freeze_stage_evidence,
    materialize_stage_evidence_gates,
    normalize_stage_evidence_links,
    reconcile_stage_evidence_links,
    stage_boundary_contract_issues,
    stage_evidence_immutability_issues,
    stage_evidence_link_issues,
    stage_evidence_sha256,
    stage_evidence_snapshot_issues,
    stage1_ledger_manifest,
    stage1_forbidden_field_issues,
    stage_codes,
    stage_evidence_contract_issues,
    stage_evidence_check_map,
    stage_evidence_gate,
    stage_evidence_readiness,
    stage_evidence_diagnostics,
    stage1_acquisition_issues,
    stage1_coverage_audit_issues,
    stage_evidence_recovery_targets,
    normalize_stage_evidence_checks,
    qualified_stage_evidence_ids,
    qualified_stage_evidence_units,
    stage1_qualification_projection,
    stage_analysis_evidence_view,
    stage_analysis_stage_context,
    stage_evidence_contract,
    build_stage1_acquisition_manifest,
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
                "signal_bindings": {
                    signal: {
                        "status": "supported",
                        "evidence_ids": ["C1"],
                        "invalid_evidence_ids": [],
                        "reason": "fixture binding",
                    }
                    for signal in stage_evidence_contract(stage).required_signals
                } if status == "present" else {},
                "evidence_strength": strength,
            }
            for stage in stage_codes()
        ]

    @staticmethod
    def _signal_bindings(stage: str, evidence_id: str, status: str = "supported") -> dict[str, object]:
        return {
            signal: {
                "status": status,
                "evidence_ids": [evidence_id] if status == "supported" else [],
                "invalid_evidence_ids": [],
                "reason": "fixture binding",
            }
            for signal in stage_evidence_contract(stage).required_signals
        }

    @staticmethod
    def _coverage_audit(checks: list[dict[str, object]]) -> dict[str, object]:
        stages: dict[str, dict[str, object]] = {}
        for check in checks:
            stage = str(check.get("stage") or "").strip().upper()
            status = str(check.get("status") or "unknown").strip().lower()
            audit_status = {
                "present": "found",
                "absent": "clear",
            }.get(status, "unknown")
            stages[stage] = {
                "status": audit_status,
                "coverage": "complete" if audit_status in {"found", "clear"} else "unknown",
                "evidence_ids": list(check.get("evidence_ids") or []),
                "observed_signals": list(check.get("observed_signals") or []),
                "missing_signals": list(check.get("missing_signals") or []),
                "signal_bindings": dict(check.get("signal_bindings") or {}),
            }
        return {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "stages": stages,
            "errors": [],
        }

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

    def test_not_needed_recovery_does_not_require_legacy_audit_projection(self) -> None:
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_recovery": {"source": "pipeline", "status": "not_needed"},
        }
        self.assertEqual(stage1_coverage_audit_issues(side, "S4"), [])

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

    def test_present_requires_each_required_signal_to_bind_to_stage_evidence(self) -> None:
        side = self._active_side("C", "present")
        s4 = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S4")
        s4.update(
            {
                "status": "present",
                "coverage": "complete",
                "evidence_ids": ["C4"],
                "observed_signals": list(stage_evidence_contract("S4").required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings("S4", "C4"),
            }
        )
        required_signal = stage_evidence_contract("S4").required_signals[0]
        s4["signal_bindings"].pop(required_signal)
        freeze_stage_evidence(side)
        issues = stage_evidence_contract_issues(side)
        self.assertIn("S4:present_missing_required_signal_bindings:" + required_signal, issues)
        self.assertEqual(stage_evidence_readiness(side, "S4"), "unknown")
        diagnostics = stage_evidence_diagnostics(side, "S4")
        self.assertIn("primary_qualification_gate", diagnostics["reason_codes"])

    def test_signal_binding_must_stay_inside_the_stage_evidence_list(self) -> None:
        checks = self._checks("present", "direct")
        checks[3]["evidence_ids"] = ["C4"]
        checks[3]["signal_bindings"] = self._signal_bindings("S4", "C5")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [
                {"id": "C4", "evidence_strength": "direct", "visual_fact": "阶段事实"},
                {"id": "C5", "evidence_strength": "direct", "visual_fact": "其他阶段事实"},
            ],
        }
        issues = stage_evidence_contract_issues(side)
        self.assertTrue(
            any(issue.startswith("S4:signal_binding_outside_stage_evidence:") for issue in issues)
        )

    def test_absent_rejects_supported_binding_without_evidence(self) -> None:
        checks = self._checks("unknown")
        required_signal = stage_evidence_contract("S4").required_signals[0]
        checks[3] = {
            "stage": "S4",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S4").required_signals),
            "signal_bindings": {
                required_signal: {
                    "status": "supported",
                    "evidence_ids": [],
                    "invalid_evidence_ids": [],
                    "reason": "无证据却声明支持",
                }
            },
        }
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [],
        }
        issues = stage_evidence_contract_issues(side)
        self.assertIn(
            f"S4:supported_signal_without_evidence:{required_signal}",
            issues,
        )
        self.assertIn("S4:absence_without_complete_coverage", issues)

    def test_primary_and_audit_signal_disagreement_is_explicit_conflict(self) -> None:
        checks = self._checks("present", "direct")
        s4 = checks[3]
        signal = stage_evidence_contract("S4").required_signals[0]
        s4["signal_bindings"][signal] = {
            "status": "supported",
            "evidence_ids": ["C1"],
            "invalid_evidence_ids": [],
            "reason": "primary",
        }
        base = self._active_side("C", "unknown")
        base["stage_evidence_checks"] = checks
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "stages": {
                "S1": {
                    "status": "found",
                    "coverage": "complete",
                    "evidence_ids": ["C1"],
                    "observed_signals": list(stage_evidence_contract("S1").required_signals),
                    "signal_bindings": self._signal_bindings("S1", "C1", "missing"),
                }
            },
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        s1 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S1")
        signal = stage_evidence_contract("S1").required_signals[0]
        self.assertEqual(s1["signal_bindings"][signal]["status"], "conflict")

    def test_legacy_coverage_merge_preserves_stage1_qualification(self) -> None:
        base = self._active_side("C", "unknown")
        base["stage1_qualification"] = {
            "source": "pipeline",
            "status": "completed",
            "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_codes": list(stage_codes()),
            "evidence_id_count": 1,
        }
        merged = _merge_video_fact_coverage_audit(
            "creator",
            base,
            {
                "version": STAGE1_COVERAGE_AUDIT_VERSION,
                "status": "completed",
                "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
                "stages": {},
            },
            self._analysis(),
        )
        self.assertEqual(merged["stage1_qualification"]["status"], "completed")

    def test_focused_recovery_closes_failed_stage1_qualification_metadata(self) -> None:
        facts = self._active_side("C", "present")
        s4 = next(item for item in facts["stage_evidence_checks"] if item["stage"] == "S4")
        s4.update(
            {
                "status": "present",
                "coverage": "complete",
                "evidence_ids": ["C4"],
                "observed_signals": list(stage_evidence_contract("S4").required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings("S4", "C4"),
            }
        )
        facts["stage1_qualification"] = {
            "source": "pipeline",
            "status": "failed",
            "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_codes": list(stage_codes()),
            "evidence_id_count": 1,
            "failure_reason": "Stage1-B response was unavailable",
        }
        freeze_stage_evidence(facts)

        recovered = _mark_stage1_qualification_recovered(facts, ["S4", "S4", "S6"])

        qualification = recovered["stage1_qualification"]
        self.assertEqual(qualification["status"], "completed")
        self.assertEqual(qualification["recovered_from"], "stage1_b_failed")
        self.assertEqual(qualification["recovered_stage_codes"], ["S4", "S6"])
        self.assertEqual(
            qualification["initial_failure_reason"],
            "Stage1-B response was unavailable",
        )

    def test_unresolved_recovery_stage_remains_failed_in_qualification_metadata(self) -> None:
        facts = self._active_side("C", "present")
        facts["stage1_qualification"] = {
            "source": "pipeline",
            "status": "completed",
            "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_codes": list(stage_codes()),
            "evidence_id_count": 6,
            "failed_stage_codes": ["S4", "S6"],
            "failure_reason": "S4/S6 qualification group failed",
        }
        freeze_stage_evidence(facts)

        recovered = _mark_stage1_qualification_recovered(facts, ["S4", "S6"])

        qualification = recovered["stage1_qualification"]
        self.assertEqual(qualification["status"], "completed")
        self.assertEqual(qualification["recovered_stage_codes"], ["S6"])
        self.assertEqual(qualification["failed_stage_codes"], ["S4"])

    def test_stage1_qualification_group_failure_is_isolated(self) -> None:
        facts = self._active_side("C")
        analysis = self._analysis()
        args = type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "llm_api_url": "https://example.invalid",
            },
        )()

        def response_for(stages: tuple[str, ...]) -> str:
            return json.dumps(
                {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "stage_evidence_checks": [
                        {
                            "stage": stage,
                            "status": "unknown",
                            "coverage": "unknown",
                            "evidence_ids": [],
                            "observed_signals": [],
                            "missing_signals": [],
                            "signal_bindings": {},
                            "reason": "fixture group completed",
                        }
                        for stage in stages
                    ],
                },
                ensure_ascii=False,
            )

        responses = [
            response_for(STAGE1_QUALIFICATION_GROUPS[0]),
            ValueError("fixture S3/S4 group failure"),
            response_for(STAGE1_QUALIFICATION_GROUPS[2]),
            response_for(STAGE1_QUALIFICATION_GROUPS[3]),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch(
            "flayr_core.llm.pipeline.fetch_json_completion",
            side_effect=responses,
        ):
            result = _run_stage1_qualification(
                args,
                analysis,
                Path(tmp),
                "",
                "creator",
                facts,
            )

        qualification = result["stage1_qualification"]
        self.assertEqual(qualification["status"], "completed")
        self.assertEqual(qualification["failed_stage_codes"], ["S3", "S4"])
        self.assertEqual(
            [record["status"] for record in qualification["group_records"]],
            ["completed", "failed", "completed", "completed"],
        )
        checks = stage_evidence_check_map(result)
        self.assertEqual(checks["S1"]["reason"], "fixture group completed")
        self.assertIn("Stage1-B 该阶段组失败", checks["S3"]["reason"])
        self.assertIn("Stage1-B 该阶段组失败", checks["S4"]["reason"])

    def test_present_qualification_uses_unit_strength_and_required_signals(self) -> None:
        checks = self._checks("present", "inferred")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{
                "id": "C1",
                "evidence_strength": "direct",
                "visual_fact": "直接可见",
                "trust_source_signals": ["independent_user"],
                "trust_source_reference": "用户评价：连续使用后体验改善。",
                "trust_source_status": "explicit_present",
            }],
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
        }
        side["stage1_coverage_audit"] = self._coverage_audit(checks)
        freeze_stage_evidence(side)
        self.assertEqual({"C1"}, qualified_stage_evidence_ids(side, "S1"))
        side["evidence_units"][0]["evidence_strength"] = "inferred"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))
        checks[0]["evidence_strength"] = "direct"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))

    def test_unbound_optional_signal_does_not_poison_required_qualification(self) -> None:
        checks = self._checks("present", "direct")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
            "stage_evidence_checks": checks,
            "evidence_units": [{
                "id": "C1",
                "evidence_strength": "direct",
                "visual_fact": "直接可见",
                "trust_source_signals": ["independent_user"],
                "trust_source_reference": "用户评价：连续使用后体验改善。",
                "trust_source_status": "explicit_present",
            }],
        }
        s3 = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S3")
        s3["observed_signals"].append("continuity")
        side["stage_evidence_checks"] = normalize_stage_evidence_checks(
            side["stage_evidence_checks"], {"C1"}
        )
        side["stage1_coverage_audit"] = self._coverage_audit(side["stage_evidence_checks"])
        freeze_stage_evidence(side)

        normalized_s3 = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S3")
        self.assertNotIn("continuity", normalized_s3["observed_signals"])
        self.assertIn("continuity", normalized_s3["unqualified_observed_signals"])
        self.assertEqual(stage_evidence_readiness(side, "S3"), "present")
        self.assertEqual(qualified_stage_evidence_ids(side, "S3"), {"C1"})
        self.assertNotIn("S3", stage_evidence_recovery_targets(side))

    def test_model_cannot_author_stage1_coverage_audit(self) -> None:
        with self.assertRaises(SystemExit):
            normalize_video_fact_result(
                "creator",
                {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
                    "stage1_coverage_audit": {
                        "version": STAGE1_COVERAGE_AUDIT_VERSION,
                        "source": "model",
                        "status": "completed",
                    },
                },
                self._analysis(),
            )

    def test_trusted_pipeline_metadata_can_only_be_restored_after_lock(self) -> None:
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
            "stage1_acquisition": {"source": "pipeline"},
        }
        with self.assertRaises(SystemExit):
            normalize_video_understanding({"creator": side})
        normalized = normalize_video_understanding(
            {"creator": side},
            trusted_stage1_acquisition={"creator": {"source": "pipeline", "version": STAGE1_ACQUISITION_VERSION}},
            allow_trusted_pipeline_metadata=True,
        )
        self.assertEqual(normalized["creator"]["stage1_acquisition"]["source"], "pipeline")

    def test_stage1_projection_is_code_owned_and_preserves_candidates(self) -> None:
        side = self._active_side("C", "present")
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
            "evidence_strength": "direct",
        }
        side["evidence_units"].append(
            {
                "id": "C_NOT_QUALIFIED",
                "time_range": "0.0s - 1.0s",
                "evidence_strength": "inferred",
                "visual_fact": "候选观察",
                "functions": ["S1_hook"],
            }
        )
        freeze_stage_evidence(side)

        projection = stage1_qualification_projection(side, ["S1"])
        stage = projection["stages"]["S1"]

        self.assertEqual(projection["version"], STAGE1_PROJECTION_VERSION)
        self.assertEqual(stage["qualified_evidence_ids"], ["C1"])
        self.assertIn("C_NOT_QUALIFIED", stage["candidate_evidence_ids"])
        self.assertEqual(stage["stage_readiness"], "present")
        self.assertEqual(stage["coverage_state"], "captured")
        self.assertEqual(stage["evidence_strength"], "direct")
        self.assertEqual(stage["projection_reason_code"], "qualified")
        self.assertEqual(stage["ledger_hash"], side["evidence_set_sha256"])

    def test_not_applicable_is_closed_without_becoming_absence(self) -> None:
        side = self._active_side("C")
        check = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S6")
        check.update(
            {
                "status": "not_applicable",
                "coverage": "complete",
                "reason": "比较合同明确该视频不涉及可执行购买行动。",
            }
        )
        freeze_stage_evidence(side)

        self.assertEqual(stage_evidence_readiness(side, "S6"), "not_applicable")
        projection = stage1_qualification_projection(side, ["S6"])["stages"]["S6"]
        self.assertEqual(projection["stage_readiness"], "not_applicable")
        self.assertEqual(projection["projection_reason_code"], "not_applicable")
        self.assertEqual(projection["qualified_evidence_ids"], [])

    def test_not_applicable_survives_focused_audit_projection(self) -> None:
        side = self._active_side("C")
        check = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S6")
        check.update(
            {
                "status": "not_applicable",
                "coverage": "complete",
                "reason": "比较合同明确该视频不涉及可执行购买行动。",
            }
        )
        projected = _materialize_stage_recovery_audit(side, ["S6"])
        side["stage1_coverage_audit"] = projected
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S6"), "not_applicable")
        self.assertEqual(projected["stages"]["S6"]["status"], "clear")
        self.assertEqual(projected["stages"]["S6"]["coverage"], "complete")
        self.assertEqual(stage1_coverage_audit_issues(side, "S6"), [])

    def test_stage_gate_closes_only_when_both_sides_are_not_applicable(self) -> None:
        creator = self._active_side("C")
        benchmark = self._active_side("B")
        for side in (creator, benchmark):
            check = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S6")
            check.update(
                {
                    "status": "not_applicable",
                    "coverage": "complete",
                    "reason": "比较合同明确该视频不涉及可执行购买行动。",
                }
            )
            freeze_stage_evidence(side)
        result = {
            "video_understanding": {
                "creator": creator,
                "benchmark": benchmark,
            }
        }
        both_closed = stage_evidence_gate(result, "S6")
        self.assertEqual(both_closed["status"], "not_applicable")
        self.assertFalse(both_closed["analysis_allowed"])

        result["video_understanding"]["benchmark"] = self._active_side("B", "present")
        mismatched = stage_evidence_gate(result, "S6")
        self.assertEqual(mismatched["status"], "blocked")
        self.assertEqual(mismatched["reason_code"], "comparison_scope_closed")
        self.assertFalse(mismatched["analysis_allowed"])

    def test_stage1_ledger_manifest_preserves_unqualified_units(self) -> None:
        side = self._active_side("C", "present")
        side["evidence_units"].append(
            {
                "id": "C_UNASSIGNED",
                "time_range": "99.0s - 100.0s",
                "evidence_strength": "direct",
                "visual_fact": "未被任何阶段引用的观察",
            }
        )
        freeze_stage_evidence(side)

        manifest = stage1_ledger_manifest(side)

        self.assertEqual(manifest["unit_count"], len(side["evidence_units"]))
        self.assertEqual(
            {item["id"] for item in manifest["units"]},
            {item["id"] for item in side["evidence_units"]},
        )
        self.assertEqual(manifest["ledger_sha256"], side["evidence_set_sha256"])

    def test_segmented_projection_rebuilds_mechanical_fields_from_qualified_facts(self) -> None:
        facts = {
            "benchmark": self._active_side("B", "present"),
            "creator": self._active_side("C", "present"),
        }
        projected = _normalize_segmented_stage(
            {
                "stage": "S6 CTA",
                "stage_state": "completed",
                "relation": "benchmark_better",
                "model_gap_magnitude": "large",
                "benchmark_evidence_ids": ["B6"],
                "creator_evidence_ids": ["C6"],
                "judgment_reason": "基于 B6 和 C6",
                "benchmark_time_range": "900s - 901s",
                "creator_summary": "模型伪造的摘要",
                "severity": "small",
            },
            "S6",
            facts,
        )

        self.assertEqual(projected["benchmark_time_range"], "5.0s - 6.0s")
        self.assertEqual(projected["creator_time_range"], "5.0s - 6.0s")
        self.assertNotEqual(projected["creator_summary"], "模型伪造的摘要")
        self.assertEqual(projected["severity"], "large")
        self.assertEqual(projected["model_severity"], "large")

    def test_closed_comparison_scope_keeps_qualified_facts_without_model_references(self) -> None:
        facts = {
            "benchmark": self._active_side("B", "present"),
            "creator": self._active_side("C", "present"),
        }
        projected = _normalize_segmented_stage(
            {
                "stage": "S6 CTA",
                "stage_state": "completed",
                "relation": "benchmark_better",
                "model_gap_magnitude": "large",
                "judgment_reason": "该阶段不可比",
            },
            "S6",
            facts,
            {
                "stage_eligibility": {
                    "S6": {
                        "status": "not_comparable",
                        "basis": "双方没有共同的信任材料合同",
                    }
                }
            },
        )

        self.assertEqual(projected["comparison_status"], "not_directly_comparable")
        self.assertEqual(projected["analysis_status"], "not_comparable")
        self.assertEqual(projected["relation"], "uncertain")
        self.assertEqual(projected["model_gap_magnitude"], "uncertain")
        self.assertTrue(projected["benchmark_evidence_ids"])
        self.assertTrue(projected["creator_evidence_ids"])

    def test_segmented_normalization_does_not_trust_model_comparison_status(self) -> None:
        result = normalize_analysis_result(
            {
                "stage2_pipeline_version": "segmented_stage_v1",
                "comparison_eligibility": {
                    "scope": "same_product",
                    "stage_eligibility": {
                        stage: {"status": "direct", "basis": "同一任务"}
                        for stage in stage_codes()
                    }
                },
                "stage_analysis": [
                    {
                        "stage": f"S{index}",
                        "comparison_status": "not_directly_comparable",
                        "model_comparison_status": "not_comparable",
                        "stage_state": "unknown",
                    }
                    for index in range(1, 7)
                ],
                "improvements": [{"title": "fixture", "time_range": "0s - 1s"}],
            }
        )
        self.assertTrue(all(
            not stage.get("comparison_status")
            and not stage.get("model_comparison_status")
            for stage in result["stage_analysis"]
        ))

    def test_segmented_import_uses_current_run_comparison_contract(self) -> None:
        result_contract = {
            "stage_eligibility": {
                stage: {"status": "not_comparable"}
                for stage in stage_codes()
            }
        }
        analysis_contract = {
            "stage_eligibility": {
                stage: {"status": "direct"}
                for stage in stage_codes()
            }
        }
        selected = _authoritative_segmented_comparison_contract(
            {"comparison_eligibility": analysis_contract},
            {"comparison_eligibility": result_contract},
        )
        self.assertEqual(
            selected["stage_eligibility"]["S3"]["status"],
            "direct",
        )

    def test_targeted_recovery_cache_scope_is_reusable_without_global_audit(self) -> None:
        side = self._active_side("C", "present")
        full_audit = side["stage1_coverage_audit"]
        side["stage1_coverage_audit"] = {
            **full_audit,
            "target_stages": ["S6"],
            "stages": {"S6": full_audit["stages"]["S6"]},
        }
        side["stage1_recovery"] = {
            "source": "pipeline",
            "status": "focused_recovery",
            "target_stages": ["S6"],
            "unresolved_stages": [],
        }
        self.assertTrue(stage1_coverage_audit_issues(side))
        self.assertEqual(_video_fact_cache_stage1_coverage_issues(side), [])

    def test_unresolved_targeted_recovery_cache_is_reusable_as_typed_unknown(self) -> None:
        side = self._active_side("C")
        full_audit = side["stage1_coverage_audit"]
        unresolved_audit = copy.deepcopy(full_audit["stages"]["S6"])
        unresolved_audit.update({"status": "unknown", "coverage": "unknown", "evidence_ids": []})
        side["stage1_coverage_audit"] = {
            **full_audit,
            "status": "partial",
            "target_stages": ["S6"],
            "stages": {"S6": unresolved_audit},
        }
        side["stage1_recovery"] = {
            "source": "pipeline",
            "status": "focused_recovery_with_unresolved",
            "target_stages": ["S6"],
            "unresolved_stages": ["S6"],
        }
        self.assertEqual(_video_fact_cache_stage1_coverage_issues(side), [])

    def test_reprojection_restores_code_owned_closed_stage_projection(self) -> None:
        facts = {
            "benchmark": self._active_side("B", "present"),
            "creator": self._active_side("C", "present"),
        }
        result = {
            "stage2_pipeline_version": "segmented_stage_v1",
            "stage_analysis": [
                {
                    "stage": "S6 CTA",
                    "stage_state": "completed",
                    "relation": "benchmark_better",
                    "model_gap_magnitude": "large",
                    "judgment_reason": "不可比",
                }
            ],
        }
        _reproject_segmented_stage_results(
            result,
            facts,
            {"stage_eligibility": {"S6": {"status": "not_comparable"}}},
        )
        stage = result["stage_analysis"][0]
        self.assertEqual(stage["comparison_status"], "not_directly_comparable")
        self.assertEqual(stage["analysis_status"], "not_comparable")
        self.assertEqual(stage["relation"], "uncertain")
        self.assertEqual(stage["benchmark_evidence_ids"], ["B6"])
        self.assertEqual(stage["creator_evidence_ids"], ["C6"])

    def test_stage1_to_stage2_handoff_is_lossless_and_tamper_detectable(self) -> None:
        facts = {
            "benchmark": self._active_side("B", "present"),
            "creator": self._active_side("C", "present"),
        }
        analysis = self._analysis()
        handoff = _build_stage1_to_stage2_handoff(facts, analysis)
        self.assertEqual(_stage1_to_stage2_handoff_issues(handoff, facts, analysis), [])

        handoff["roles"]["creator"]["ledger_manifest"]["units"].pop()
        self.assertIn(
            "creator:handoff_field_mismatch:ledger_manifest",
            _stage1_to_stage2_handoff_issues(handoff, facts, analysis),
        )

    def test_active_contract_rejects_forbidden_field_in_imported_side(self) -> None:
        side = self._active_side("C")
        side["stage_analysis"] = [{"stage": "S1", "severity": "large"}]
        issues = stage_evidence_contract_issues(side)
        self.assertIn("forbidden_stage1_field:stage_analysis", issues)
        self.assertIn("forbidden_stage1_field:stage_analysis[0].severity", issues)

    def test_missing_coverage_audit_blocks_every_active_stage(self) -> None:
        side = self._active_side("C", "present")
        side.pop("stage1_coverage_audit", None)
        freeze_stage_evidence(side)
        self.assertEqual(
            stage_evidence_recovery_targets(side, include_budget=False),
            list(stage_codes()),
        )
        for stage in stage_codes():
            self.assertEqual(stage_evidence_readiness(side, stage), "unknown")
            self.assertIn(f"{stage}:coverage_audit_missing_or_old", stage1_coverage_audit_issues(side, stage))

    def test_gate_diagnostics_identify_only_the_affected_stage(self) -> None:
        side = self._active_side("C", "present")
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
        }
        side["stage1_coverage_audit"]["stages"].pop("S4")
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_diagnostics(side, "S1")["reason_codes"], ["ready"])
        s4 = stage_evidence_diagnostics(side, "S4")
        self.assertEqual(s4["status"], "unknown")
        self.assertIn("coverage_audit_gate", s4["reason_codes"])
        self.assertNotIn("coverage_audit_gate", stage_evidence_diagnostics(side, "S1")["reason_codes"])

    def test_gate_diagnostics_distinguish_acquisition_and_coverage_failures(self) -> None:
        side = self._active_side("C")
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S1").required_signals),
        }
        side["stage1_coverage_audit"]["stages"]["S1"]["status"] = "clear"
        side["stage1_coverage_audit"]["stages"]["S1"]["coverage"] = "complete"
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        freeze_stage_evidence(side)
        diagnostics = stage_evidence_diagnostics(side, "S1")
        self.assertEqual(diagnostics["status"], "unknown")
        self.assertIn("acquisition_gate", diagnostics["reason_codes"])
        self.assertNotIn("coverage_audit_gate", diagnostics["reason_codes"])

    def test_gate_diagnostics_are_persisted_for_both_roles(self) -> None:
        result = {
            "stage_analysis": [{"stage": "S1"}],
            "video_understanding": {
                "creator": self._active_side("C", "present"),
                "benchmark": self._active_side("B", "unknown"),
            },
        }
        result["video_understanding"]["creator"]["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
        }
        freeze_stage_evidence(result["video_understanding"]["creator"])
        materialize_stage_evidence_gates(result)
        gate = result["stage_analysis"][0]["stage_evidence_gate"]
        self.assertEqual(gate["creator"]["diagnostics"]["reason_codes"], ["ready"])
        self.assertIn("primary_unknown", gate["benchmark"]["diagnostics"]["reason_codes"])

    def test_failed_coverage_audit_is_structured_and_blocks_all_stages(self) -> None:
        side = self._active_side("C", "present")
        atomic_units = copy.deepcopy(side["evidence_units"])
        result = _mark_video_fact_coverage_audit_failed(
            side,
            target_stages=list(stage_codes()),
            trigger_reasons=["stage_evidence_incomplete"],
            contract_issues=[],
            budget_flag=False,
            error=ValueError("temporary audit failure"),
            api_key="secret-key",
        )
        self.assertIs(result, side)
        self.assertEqual(result["stage1_coverage_audit"]["status"], "failed")
        self.assertEqual(result["stage1_recovery"]["status"], "coverage_audited_with_unresolved")
        self.assertEqual(result["stage1_recovery"]["failure_reason"], "temporary audit failure")
        self.assertNotIn("secret-key", str(result))
        self.assertEqual(result["evidence_units"], atomic_units)
        self.assertEqual(stage_evidence_contract_issues(result), [])
        for stage in stage_codes():
            check = next(item for item in result["stage_evidence_checks"] if item["stage"] == stage)
            self.assertEqual(check["status"], "unknown")
            self.assertEqual(check["evidence_ids"], [])
            self.assertEqual(check["signal_bindings"], {})
            self.assertEqual(stage_evidence_readiness(result, stage), "unknown")
            self.assertIn(f"{stage}:coverage_audit_not_completed", stage1_coverage_audit_issues(result, stage))

    def test_focused_recovery_execution_uses_registered_targets(self) -> None:
        """The live Stage1-C path reaches the append-only merge safely."""
        facts = self._active_side("C")
        args = type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "llm_api_url": "https://example.invalid/api",
                "_resource_budget": None,
            },
        )()
        audit_response = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "candidate_evidence_units": [],
            "stage_evidence_checks": [
                {
                    "stage": stage,
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                }
                for stage in stage_codes()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_recovery_payload",
                return_value={"messages": []},
            ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                return_value=json.dumps(audit_response),
            ):
                result = _maybe_recover_video_facts(
                    args,
                    self._analysis(),
                    Path(tmp_dir),
                    "secret",
                    "creator",
                    facts,
                )
        self.assertEqual(result["stage1_recovery"]["status"], "focused_recovery_with_unresolved")
        self.assertEqual(result["stage1_coverage_audit"]["status"], "partial")
        self.assertEqual(result["stage1_recovery"]["recovery_mode"], "stage1_c_focused_once")

    def test_recovery_windows_use_canonical_stage_codes_and_merge_adjacent_stages(self) -> None:
        analysis = {
            "videos": {
                "creator": {
                    "duration_seconds": 60,
                }
            }
        }
        windows = _recovery_stage_windows(analysis, "creator", ["S2", "S3", "S6"])
        self.assertEqual([item[0] for item in windows], ["S2", "S6"])
        self.assertLessEqual(windows[0][1], 3.0)
        self.assertGreaterEqual(windows[0][2], 15.0)
        self.assertEqual(windows[1][2], 60.0)

    def test_recovery_media_never_falls_back_to_full_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            video_path = root / "creator.mp4"
            video_path.write_bytes(b"fixture")
            analysis = {
                "videos": {
                    "creator": {
                        "duration_seconds": 60,
                        "path": str(video_path),
                        "work_dir": str(root),
                    }
                }
            }
            media = [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,frame"}},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,full"}},
                {"type": "input_audio", "input_audio": {"data": "full-audio", "format": "wav"}},
            ]
            with patch("flayr_core.llm.payload.can_analyze_native_audio", return_value=True), patch(
                "flayr_core.llm.payload.video_to_data_url",
                return_value="data:video/mp4;base64,clip",
            ) as clip:
                result = _replace_recovery_full_media(
                    media,
                    analysis,
                    "creator",
                    ["S3"],
                    api_url="https://example.invalid/api",
                    model="test-model",
                    budget=None,
                )

            self.assertEqual(sum(item.get("type") == "video_url" for item in result), 1)
            self.assertNotIn("full", json.dumps(result))
            clip.assert_called_once()
            self.assertGreater(clip.call_args.kwargs["duration"], 0)
            self.assertLess(clip.call_args.kwargs["start"], 15.0)

    def test_focused_recovery_preserves_candidate_without_qualifying_it(self) -> None:
        facts = self._active_side("C")
        args = type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "llm_api_url": "https://example.invalid/api",
                "_resource_budget": None,
                "llm_image_limit": 4,
            },
        )()
        recovery_response = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "candidate_evidence_units": [
                {
                    "id": "C_REC_S4",
                    "time_range": "3.0s - 4.0s",
                    "information": "定向补观察到一段可能与效果有关的画面。",
                    "visual_fact": "效果区域可见，但因果关系仍待资格判断。",
                    "evidence_strength": "direct",
                    "functions": ["S4_effect"],
                }
            ],
            "stage_evidence_checks": [
                {
                    "stage": "S4",
                    "status": "unknown",
                    "coverage": "partial",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": ["effect_attribution"],
                    "signal_bindings": {},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_recovery_payload",
                return_value={"messages": []},
            ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                return_value=json.dumps(recovery_response),
            ):
                result = _maybe_recover_video_facts(
                    args,
                    self._analysis(),
                    Path(tmp_dir),
                    "secret",
                    "creator",
                    facts,
                )

        self.assertIn("C_REC_S4", {item["id"] for item in result["evidence_units"]})
        s4 = next(item for item in result["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "unknown")
        view = stage_analysis_evidence_view(result, {"S4"})
        self.assertEqual(
            [item["id"] for item in view["candidate_observations_by_stage"]["S4"]],
            ["C_REC_S4"],
        )
        self.assertEqual(view["qualified_stage_evidence_ids"]["S4"], [])

    def test_recovery_blocks_only_stage_with_remaining_structural_issue(self) -> None:
        facts = self._active_side("C", "unknown")
        facts["evidence_units"][4].update(
            {
                "trust_source_signals": ["independent_user"],
                "trust_source_reference": "用户评价：连续使用后体验改善。",
                "trust_source_status": "explicit_present",
            }
        )
        facts["stage_evidence_checks"] = [
            {
                "stage": stage,
                "status": "present",
                "coverage": "complete",
                "evidence_ids": [f"C{index}"],
                "observed_signals": list(stage_evidence_contract(stage).required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings(stage, f"C{index}"),
            }
            for index, stage in enumerate(stage_codes(), start=1)
        ]
        s4 = facts["stage_evidence_checks"][3]
        s4["observed_signals"] = list(stage_evidence_contract("S4").required_signals)
        s4["signal_bindings"].pop("effect_attribution")
        args = type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "llm_api_url": "https://example.invalid/api",
                "_resource_budget": None,
            },
        )()
        recovery_response = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "candidate_evidence_units": [],
            "stage_evidence_checks": [
                {
                    "stage": "S4",
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": ["effect_attribution"],
                    "signal_bindings": {},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_recovery_payload",
                return_value={"messages": []},
            ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                return_value=json.dumps(recovery_response),
            ):
                result = _maybe_recover_video_facts(
                    args,
                    self._analysis(),
                    Path(tmp_dir),
                    "secret",
                    "creator",
                    facts,
                )

        checks = {item["stage"]: item for item in result["stage_evidence_checks"]}
        self.assertEqual(checks["S4"]["status"], "unknown")
        self.assertEqual(checks["S4"]["evidence_ids"], [])
        for stage in {"S1", "S2", "S3", "S5", "S6"}:
            self.assertEqual(checks[stage]["status"], "present")
        self.assertEqual(result["stage1_coverage_audit"]["status"], "partial")
        self.assertIn("S4", result["stage1_recovery"]["unresolved_stages"])
        self.assertEqual(stage_evidence_contract_issues(result), [])

    def test_coverage_audit_rejects_nested_downstream_judgment_fields(self) -> None:
        facts = self._active_side("C")
        args = type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "llm_api_url": "https://example.invalid/api",
                "_resource_budget": None,
            },
        )()
        audit_response = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "candidate_evidence_units": [],
            "stage_evidence_checks": [
                {
                    "stage": stage,
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "reason": {"severity": "large"} if stage == "S4" else "未确认",
                }
                for stage in stage_codes()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_recovery_payload",
                return_value={"messages": []},
            ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                return_value=json.dumps(audit_response),
            ):
                result = _maybe_recover_video_facts(
                    args,
                    self._analysis(),
                    Path(tmp_dir),
                    "secret",
                    "creator",
                    facts,
                )
        self.assertEqual(result["stage1_coverage_audit"]["status"], "partial")
        self.assertEqual(result["stage1_recovery"]["status"], "focused_recovery_with_unresolved")
        self.assertIn("returned downstream fields", result["stage1_recovery"]["failure_reason"])
        self.assertIn("stage_evidence_checks[3].reason.severity", result["stage1_recovery"]["failure_reason"])

    def test_alignment_uses_final_readiness_after_coverage_audit_failure(self) -> None:
        sides = {
            role: _mark_video_fact_coverage_audit_failed(
                self._active_side(role_code, "present"),
                target_stages=list(stage_codes()),
                trigger_reasons=["stage_evidence_incomplete"],
                contract_issues=[],
                budget_flag=False,
                error=ValueError("audit unavailable"),
                api_key="",
            )
            for role, role_code in (("creator", "C"), ("benchmark", "B"))
        }
        result = {
            "video_understanding": sides,
            "stage_analysis": [{"stage": stage} for stage in stage_codes()],
        }
        # Primary S6 is present, but the failed semantic audit makes the
        # final handoff unknown. Validation must allow the blocked stage to
        # carry no publishable references instead of requesting a repair loop.
        validate_evidence_alignment(result)

    def test_independent_audit_can_recover_a_missing_positive_fact(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        base["stage_evidence_checks"] = checks
        base["stage1_coverage_audit"] = self._coverage_audit(checks)
        contract = stage_evidence_contract("S4")
        candidate = {
            "id": "C_A1",
            "time_range": "3s - 4s",
            "information": "独立扫描发现结果差异与产品动作的连续证据",
            "visual_fact": "产品操作后目标区域出现可见结果差异",
            "evidence_strength": "direct",
        }
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "candidate_evidence_units": [candidate],
            "stages": {
                "S4": {
                    "status": "found",
                    "coverage": "complete",
                    "evidence_ids": ["C_A1"],
                    "observed_signals": list(contract.required_signals),
                    "missing_signals": [],
                    "signal_bindings": self._signal_bindings("S4", "C_A1"),
                }
            },
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        freeze_stage_evidence(merged)
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "present")
        self.assertEqual(qualified_stage_evidence_ids(merged, "S4"), {"C_A1"})
        self.assertEqual(stage_evidence_readiness(merged, "S4"), "present")

    def test_complete_audit_closes_partial_coverage_without_rewriting_primary_fact(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        contract = stage_evidence_contract("S4")
        checks[3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "partial",
            "evidence_ids": ["C4"],
            "observed_signals": list(contract.required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S4", "C4"),
        }
        base["stage_evidence_checks"] = checks
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "candidate_evidence_units": [{
                "id": "C_A1",
                "time_range": "3s - 4s",
                "information": "独立扫描确认该阶段事实",
                "visual_fact": "产品操作后目标区域出现可见结果差异",
                "evidence_strength": "direct",
            }],
            "stages": {
                "S4": {
                    "status": "found",
                    "coverage": "complete",
                    "evidence_ids": ["C_A1"],
                    "observed_signals": list(contract.required_signals),
                    "missing_signals": [],
                    "signal_bindings": self._signal_bindings("S4", "C_A1"),
                }
            },
        }
        original_primary = {
            item["id"]: copy.deepcopy(item)
            for item in base["evidence_units"]
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "present")
        self.assertEqual(s4["coverage"], "complete")
        merged_primary = {
            item["id"]: item
            for item in merged["evidence_units"]
            if item["id"] in original_primary
        }
        self.assertEqual(set(merged_primary), set(original_primary))
        for evidence_id, original in original_primary.items():
            self.assertEqual(merged_primary[evidence_id]["time_range"], original["time_range"])
            self.assertEqual(merged_primary[evidence_id]["visual_fact"], original["visual_fact"])
            self.assertEqual(merged_primary[evidence_id]["evidence_strength"], original["evidence_strength"])
        self.assertNotIn("S4:present_without_complete_coverage", stage_evidence_contract_issues(merged))

    def test_partial_audit_does_not_close_partial_primary_coverage(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        contract = stage_evidence_contract("S4")
        checks[3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "partial",
            "evidence_ids": ["C4"],
            "observed_signals": list(contract.required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S4", "C4"),
        }
        base["stage_evidence_checks"] = checks
        merged = _merge_video_fact_coverage_audit(
            "creator",
            base,
            {
                "version": STAGE1_COVERAGE_AUDIT_VERSION,
                "status": "partial",
                "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
                "stages": {
                    "S4": {
                        "status": "found",
                        "coverage": "partial",
                        "evidence_ids": [],
                        "observed_signals": [],
                        "missing_signals": [],
                    }
                },
            },
            self._analysis(),
        )
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["coverage"], "partial")
        self.assertIn("S4:present_without_complete_coverage", stage_evidence_contract_issues(merged))

    def test_incomplete_independent_audit_cannot_turn_absence_into_presence(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        checks[3] = {
            "stage": "S4",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S4").required_signals),
        }
        base["stage_evidence_checks"] = checks
        base["stage1_coverage_audit"] = self._coverage_audit(checks)
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "candidate_evidence_units": [{
                "id": "C_A1",
                "time_range": "3s - 4s",
                "information": "只看到结果画面",
                "visual_fact": "结果画面可见，但未看到完整因果链",
                "evidence_strength": "direct",
            }],
            "stages": {
                "S4": {
                    "status": "found",
                    "coverage": "partial",
                    "evidence_ids": ["C_A1"],
                    "observed_signals": ["result_difference"],
                    "missing_signals": ["effect_attribution"],
                }
            },
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        freeze_stage_evidence(merged)
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "conflict")
        self.assertEqual(stage_evidence_readiness(merged, "S4"), "conflict")

    def test_complete_clear_audit_can_close_unknown_as_absent(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        base["stage_evidence_checks"] = checks
        base["stage1_coverage_audit"] = self._coverage_audit(checks)
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "stages": {
                "S4": {
                    "status": "clear",
                    "coverage": "complete",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": list(stage_evidence_contract("S4").required_signals),
                }
            },
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        freeze_stage_evidence(merged)
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "absent")
        self.assertEqual(stage_evidence_readiness(merged, "S4"), "absent")

    def test_primary_and_audit_disagreement_is_conflict_not_order_dependent(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        checks[3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C4"],
            "observed_signals": list(stage_evidence_contract("S4").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S4", "C4"),
        }
        base["stage_evidence_checks"] = checks
        base["stage1_coverage_audit"] = self._coverage_audit(checks)
        audit = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "stages": {
                "S4": {
                    "status": "clear",
                    "coverage": "complete",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": list(stage_evidence_contract("S4").required_signals),
                }
            },
        }
        merged = _merge_video_fact_coverage_audit("creator", base, audit, self._analysis())
        freeze_stage_evidence(merged)
        self.assertEqual(stage_evidence_readiness(merged, "S4"), "unknown")
        self.assertIn("S4:coverage_audit_disagrees_with_present", stage1_coverage_audit_issues(merged, "S4"))

    def test_coverage_audit_cannot_overwrite_primary_conflict(self) -> None:
        base = self._active_side("C", "unknown")
        checks = self._checks("unknown")
        checks[3] = {
            "stage": "S4",
            "status": "conflict",
            "coverage": "unknown",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": [],
        }
        base["stage_evidence_checks"] = checks
        base["stage1_coverage_audit"] = self._coverage_audit(checks)
        merged = _merge_video_fact_coverage_audit(
            "creator",
            base,
            {
                "version": STAGE1_COVERAGE_AUDIT_VERSION,
                "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
                "stages": {
                    "S4": {
                        "status": "clear",
                        "coverage": "complete",
                        "evidence_ids": [],
                        "observed_signals": [],
                        "missing_signals": list(stage_evidence_contract("S4").required_signals),
                    }
                },
            },
            self._analysis(),
        )
        s4 = next(item for item in merged["stage_evidence_checks"] if item["stage"] == "S4")
        self.assertEqual(s4["status"], "conflict")
        freeze_stage_evidence(merged)
        self.assertEqual(stage_evidence_readiness(merged, "S4"), "conflict")

    def test_unknown_or_conflict_cannot_carry_evidence_ids(self) -> None:
        checks = self._checks("unknown")
        checks[0]["evidence_ids"] = ["C1"]
        checks[1]["status"] = "conflict"
        checks[1]["evidence_ids"] = ["C1"]
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{
                "id": "C1",
                "evidence_strength": "direct",
                "visual_fact": "直接可见",
                "trust_source_signals": ["independent_user"],
                "trust_source_reference": "用户评价：连续使用后体验改善。",
                "trust_source_status": "explicit_present",
            }],
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

    def test_s5_present_requires_typed_source_in_atomic_fact(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage_evidence_checks"][4] = {
            "stage": "S5",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C5"],
            "observed_signals": list(stage_evidence_contract("S5").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S5", "C5"),
        }
        self.assertIn(
            "S5:present_without_typed_trust_source",
            stage_evidence_contract_issues(side),
        )

        unit = side["evidence_units"][4]
        unit["trust_source_signals"] = ["independent_user"]
        unit["trust_source_reference"] = "用户评价：连续使用一周后脚后跟不再开裂。"
        unit["trust_source_status"] = "explicit_present"
        self.assertNotIn(
            "S5:present_without_typed_trust_source",
            stage_evidence_contract_issues(side),
        )

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

    def test_model_cannot_author_budget_exhaustion_state(self) -> None:
        with self.assertRaisesRegex(SystemExit, "管线字段"):
            normalize_video_fact_result(
                "creator",
                {
                    "evidence_units": [{"id": "C1", "information": "观察"}],
                    "evidence_budget_exceeded": True,
                },
                self._analysis(),
            )

    def test_stage1_recovery_is_append_only_for_existing_units(self) -> None:
        base = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [{"id": "C1", "time_range": "0s - 1s", "information": "原始事实"}],
            },
            self._analysis(),
        )
        recovery = {
            "candidate_evidence_units": [{"id": "CR1", "time_range": "1s - 2s", "information": "补充事实"}],
            "stage_evidence_checks": [],
        }
        merged = _merge_video_fact_recovery("creator", base, recovery, self._analysis(), [])
        self.assertEqual(merged["evidence_units"][0], base["evidence_units"][0])
        self.assertEqual(merged["evidence_units"][1]["id"], "CR1")

    def test_stage1_recovery_invalid_candidate_id_cannot_collide_with_ledger(self) -> None:
        base = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [{"id": "C1", "time_range": "0s - 1s", "information": "原始事实"}],
            },
            self._analysis(),
        )
        merged = _merge_video_fact_recovery(
            "creator",
            base,
            {
                "candidate_evidence_units": [
                    {"id": "not-a-valid-ledger-id", "time_range": "1s - 2s", "information": "补充事实"}
                ],
                "stage_evidence_checks": [],
            },
            self._analysis(),
            [],
        )
        ids = [item["id"] for item in merged["evidence_units"]]
        self.assertEqual(ids, ["C1", "C2"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_fact_time_range_clamps_only_endpoint_rounding_noise(self) -> None:
        analysis = {
            "videos": {
                "benchmark": {},
                "creator": {"duration_seconds": 53.766667},
            }
        }
        normalized = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [
                    {"id": "C1", "time_range": "47.0s - 53.8s", "information": "末尾观察"},
                    {"id": "C2", "time_range": "47.0s - 54.0s", "information": "真实越界"},
                ]
            },
            analysis,
        )
        normalized_range = normalized["evidence_units"][0]["time_range"]
        self.assertEqual(normalized_range, "47.00s - 53.766667s")
        self.assertEqual(
            parse_time_range_seconds(normalized_range, 53.766667),
            (47.0, 53.766667),
        )
        self.assertEqual(normalized["evidence_units"][1]["time_range"], "47.0s - 54.0s")

    def test_stage1_fact_voiceover_is_clipped_to_its_word_timed_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "transcript.txt"
            transcript.write_text("hook problem later cta", encoding="utf-8")
            words = root / "transcript.words.json"
            words.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 0.1, "end_seconds": 0.4, "text": "hook"},
                            {"start_seconds": 0.4, "end_seconds": 0.8, "text": "problem"},
                            {"start_seconds": 8.0, "end_seconds": 8.4, "text": "later"},
                            {"start_seconds": 40.0, "end_seconds": 40.4, "text": "cta"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            analysis = {
                "videos": {
                    "benchmark": {},
                    "creator": {
                        "work_dir": str(root),
                        "duration_seconds": 45.0,
                        "transcript_path": str(transcript),
                        "transcript_words_path": str(words),
                    },
                }
            }
            normalized = normalize_video_fact_result(
                "creator",
                {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": "0.0s - 2.0s",
                            "information": "开场事实",
                            "voiceover": "hook problem later cta",
                            "voiceover_zh": "包含整片后续内容的翻译",
                        }
                    ]
                },
                analysis,
            )

        self.assertEqual(normalized["evidence_units"][0]["voiceover"], "hook problem")
        self.assertEqual(normalized["evidence_units"][0]["voiceover_zh"], "")

    def test_budget_flag_opens_recovery_for_every_registered_stage(self) -> None:
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
            "stage_evidence_checks": self._checks("present", "direct"),
            "evidence_units": [{
                "id": "C1",
                "evidence_strength": "direct",
                "visual_fact": "直接可见",
                "trust_source_signals": ["independent_user"],
                "trust_source_reference": "用户评价：连续使用后体验改善。",
                "trust_source_status": "explicit_present",
            }],
            "evidence_budget_exceeded": True,
        }
        side["stage1_coverage_audit"] = self._coverage_audit(side["stage_evidence_checks"])
        self.assertEqual(stage_evidence_recovery_targets(side), list(stage_codes()))
        self.assertEqual(stage_evidence_recovery_targets(side, include_budget=False), [])

    def test_budget_flag_is_not_qualified_until_pipeline_recovery_finishes(self) -> None:
        checks = self._checks("present", "direct")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
            "evidence_budget_exceeded": True,
        }
        side["stage1_coverage_audit"] = self._coverage_audit(side["stage_evidence_checks"])
        freeze_stage_evidence(side)
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
                "stage1_acquisition": self._active_side(role_code)["stage1_acquisition"],
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
        freeze_stage_evidence(side)
        view = stage_analysis_evidence_view({"creator": side, "benchmark": side})
        self.assertEqual({"C6"}, {unit["id"] for unit in view["creator"]["evidence_units"]})
        self.assertNotIn("attention_scan_audit", view["creator"])
        self.assertNotIn("content_summary", view["creator"])
        self.assertNotIn("structure_event_checks", view["creator"])
        self.assertNotIn("stage1_acquisition", view["creator"])
        self.assertNotIn("stage1_coverage_audit", view["creator"])
        self.assertNotIn("evidence_set_sha256", view["creator"])
        self.assertEqual(view["creator"]["analysis_evidence_scope"], "qualified_stage_evidence_only")

        side["evidence_budget_exceeded"] = True
        blocked = stage_analysis_evidence_view({"creator": side})
        self.assertEqual(blocked["creator"]["evidence_units"], [])
        s6_check = next(item for item in blocked["creator"]["stage_evidence_checks"] if item["stage"] == "S6")
        self.assertEqual(s6_check["status"], "unknown")
        self.assertEqual(blocked["creator"]["stage_evidence_readiness"]["S6"], "unknown")

    def test_analysis_view_preserves_coverage_audit_candidates_for_recovery(self) -> None:
        side = self._active_side("C")
        side["evidence_units"].append(
            {
                "id": "C_AUDIT_S4",
                "time_range": "3.0s - 4.0s",
                "evidence_strength": "direct",
                "visual_fact": "独立覆盖审计发现的效果候选事实",
                "functions": [],
            }
        )
        side["stage1_coverage_audit"]["stages"]["S4"]["evidence_ids"] = ["C_AUDIT_S4"]

        view = stage_analysis_evidence_view({"creator": side}, ["S4"])

        self.assertEqual(
            view["creator"]["candidate_evidence_ids_by_stage"]["S4"],
            ["C_AUDIT_S4"],
        )
        self.assertEqual(
            view["creator"]["candidate_observations_by_stage"]["S4"][0]["id"],
            "C_AUDIT_S4",
        )
        self.assertEqual(view["creator"]["stage_evidence_units"]["S4"], [])

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
            "signal_bindings": self._signal_bindings("S1", "C1"),
        }
        checks[5] = {
            "stage": "S6",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C6"],
            "observed_signals": list(stage_evidence_contract("S6").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S6", "C6"),
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
        freeze_stage_evidence(side)
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
                        "trust_source_signals": ["independent_user"],
                        "trust_source_reference": "用户评价：连续使用后体验改善。",
                        "trust_source_status": "explicit_present",
                    },
                ],
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
                "stage_evidence_checks": self._checks("present", "direct"),
            },
            self._analysis(),
            allow_trusted_pipeline_metadata=True,
        )
        base["stage1_acquisition"] = self._active_side("C")["stage1_acquisition"]
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
                    "signal_bindings": self._signal_bindings("S1", "C1"),
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
        freeze_stage_evidence(side)
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
            "signal_bindings": self._signal_bindings("S4", "C4"),
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
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
        }
        side["stage1_coverage_audit"] = self._coverage_audit(checks)
        freeze_stage_evidence(side)
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
            "signal_bindings": self._signal_bindings("S3", "C1"),
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
        freeze_stage_evidence(facts["creator"])
        freeze_stage_evidence(facts["benchmark"])
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
                check["signal_bindings"] = self._signal_bindings(check["stage"], unit_id)
            sides["creator" if role_code == "C" else "benchmark"] = {
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "stage1_acquisition": self._active_side(role_code)["stage1_acquisition"],
                "stage_evidence_checks": checks,
                "evidence_units": [{
                    "id": unit_id,
                    "evidence_strength": "direct",
                    "visual_fact": "画面事实",
                    "trust_source_signals": ["independent_user"],
                    "trust_source_reference": "用户评价：连续使用后体验改善。",
                    "trust_source_status": "explicit_present",
                }],
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

    def test_duplicate_model_ids_are_disambiguated_without_losing_units(self) -> None:
        units = [
            {"id": "C1", "time_range": "0s - 1s", "information": "第一条"},
            {"id": "C1", "time_range": "1s - 2s", "information": "第二条"},
        ]
        normalized = normalize_video_fact_result(
            "creator",
            {"evidence_units": units},
            self._analysis(),
        )
        self.assertEqual([item["id"] for item in normalized["evidence_units"]], ["C1", "C2"])
        self.assertEqual(
            [item["information"] for item in normalized["evidence_units"]],
            ["第一条", "第二条"],
        )

    def test_empty_or_non_object_evidence_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "no valid evidence units"):
            normalize_video_fact_result(
                "creator",
                {"evidence_units": [None, "not-an-object"]},
                self._analysis(),
            )

    def test_model_cannot_claim_pipeline_recovery_status(self) -> None:
        with self.assertRaisesRegex(SystemExit, "stage1_recovery"):
            normalize_video_fact_result(
                "creator",
                {
                    "evidence_units": [{"id": "C1", "information": "观察"}],
                    "stage1_recovery": {"source": "pipeline", "status": "applied"},
                },
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
                "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
                "stage_evidence_checks": self._checks("unknown"),
            },
            self._analysis(),
            allow_trusted_pipeline_metadata=True,
        )
        base["stage1_acquisition"] = self._active_side("C")["stage1_acquisition"]
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
                    "signal_bindings": self._signal_bindings("S1", "C2"),
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
                        "signal_bindings": {
                            signal: {
                                "status": "supported",
                                "evidence_ids": [f"{role_code}6"],
                                "invalid_evidence_ids": [],
                                "reason": "fixture binding",
                            }
                            for signal in stage_evidence_contract("S6").required_signals
                        },
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
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_acquisition": {
                "version": STAGE1_ACQUISITION_VERSION,
                "source": "pipeline",
                "status": "complete",
                "input_mode": "canonical_frames",
                "speech_mode": "visual_driven",
                "duration_seconds": 6.0,
                "channels": {
                    "visual": {"status": "ready", "coverage": "full", "count": 6, "boundary_precision": "frame"},
                    "voiceover": {"status": "unknown", "coverage": "unknown", "count": 0},
                    "subtitle": {"status": "unknown", "coverage": "unknown", "count": 0},
                    "audio": {"status": "ready", "coverage": "full", "count": 1},
                },
                "stage_coverage": {
                    stage: {"status": "observed", "count": 1}
                    for stage in stage_codes()
                },
                "visual_input_timestamps": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
                "errors": [],
            },
            "stage_evidence_checks": checks,
            "stage1_coverage_audit": {
                "version": STAGE1_COVERAGE_AUDIT_VERSION,
                "source": "pipeline",
                "status": "completed",
                "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
                "stages": {
                    stage: {
                        "status": "found",
                        "coverage": "complete",
                        "evidence_ids": [f"{role_code}{index}"],
                        "observed_signals": list(stage_evidence_contract(stage).required_signals),
                        "missing_signals": [],
                        "signal_bindings": StageEvidenceContractTests._signal_bindings(stage, f"{role_code}{index}"),
                    }
                    for index, stage in enumerate(stage_codes(), start=1)
                },
                "errors": [],
            },
            "evidence_units": [
                {
                    "id": f"{role_code}{index}",
                    "time_range": f"{index - 1}.0s - {index}.0s",
                    "evidence_strength": "direct",
                    "visual_fact": "结尾画面可见行动入口" if stage == "S6" else f"{stage} 观察事实",
                }
                for index, stage in enumerate(stage_codes(), start=1)
            ],
        }
        freeze_stage_evidence(side)
        return side

    def test_stage2_citations_follow_stage1_readiness_per_role(self) -> None:
        creator = self._active_side("C")
        creator_s2 = next(item for item in creator["stage_evidence_checks"] if item["stage"] == "S2")
        creator_s2.update(
            {
                "status": "present",
                "coverage": "complete",
                "evidence_ids": ["C2"],
                "observed_signals": list(stage_evidence_contract("S2").required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings("S2", "C2"),
            }
        )
        freeze_stage_evidence(creator)
        benchmark = self._active_side("B")
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        positive_flag = {
            "exists": True,
            "merged_with_s3": False,
            "handoff_met": True,
            "s1_s2_compatible": True,
            "product_identity_clear": True,
            "product_role_clear": True,
            "excluded_or_risky_module": False,
            "module_type": "A",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "handoff_reason": "产品身份与用途承接钩子。",
            "evidence_ids": [],
        }
        stages[1].update(
            {
                "creator_evidence_ids": ["C3"],
                "benchmark_evidence_ids": ["B2"],
                "creator_s2": copy.deepcopy(positive_flag),
                "benchmark_s2": copy.deepcopy(positive_flag),
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {"creator": creator, "benchmark": benchmark},
        }

        align_stage_flag_evidence(result)

        self.assertEqual(stages[1]["creator_evidence_ids"], ["C2"])
        self.assertEqual(stages[1]["creator_s2"]["evidence_ids"], ["C2"])
        self.assertEqual(stages[1]["benchmark_evidence_ids"], [])
        self.assertEqual(stages[1]["benchmark_s2"]["evidence_ids"], [])
        validate_s2_contract_flags(result, {"s2_flags_required": True})

    def test_stage_quote_and_range_are_bound_to_locked_stage1_units(self) -> None:
        creator = self._active_side("C")
        creator_s3 = next(item for item in creator["stage_evidence_checks"] if item["stage"] == "S3")
        creator_s3.update(
            {
                "status": "present",
                "coverage": "complete",
                "evidence_ids": ["C3"],
                "observed_signals": list(stage_evidence_contract("S3").required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings("S3", "C3"),
            }
        )
        creator_unit = next(item for item in creator["evidence_units"] if item["id"] == "C3")
        creator_unit.update(
            {
                "time_range": "12.0s - 18.0s",
                "voiceover": "挤一点在手上，再涂到脚后跟。",
                "voiceover_zh": "挤一点在手上，再涂到脚后跟。",
            }
        )
        creator["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "ready",
            "coverage": "full",
            "count": 1,
            "boundary_precision": "word",
        }
        freeze_stage_evidence(creator)
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[2].update(
            {
                "creator_time_range": "9.0s - 10.0s",
                "creator_quote": "来自窗口外的模型口播",
                "creator_quote_zh": "来自窗口外的模型口播",
                "benchmark_time_range": "9.0s - 10.0s",
                "benchmark_quote": "未资格化口播",
                "benchmark_quote_zh": "未资格化口播",
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": creator,
                "benchmark": self._active_side("B"),
            },
        }

        bind_timed_transcript_quotes(result, {})

        self.assertEqual(stages[2]["creator_time_range"], "12.0s - 18.0s")
        self.assertEqual(stages[2]["creator_quote"], "挤一点在手上，再涂到脚后跟。")
        self.assertEqual(stages[2]["benchmark_quote"], "")
        self.assertEqual(stages[2]["benchmark_quote_zh"], "")

    def test_explicit_absence_does_not_hide_positive_s2_without_evidence(self) -> None:
        creator = self._active_side("C")
        creator_check = next(item for item in creator["stage_evidence_checks"] if item["stage"] == "S2")
        creator_check.update(
            {
                "status": "absent",
                "coverage": "complete",
                "evidence_ids": [],
                "observed_signals": [],
                "missing_signals": list(stage_evidence_contract("S2").required_signals),
                "signal_bindings": {},
            }
        )
        creator_audit = creator["stage1_coverage_audit"]["stages"]["S2"]
        creator_audit.update(
            {
                "status": "clear",
                "coverage": "complete",
                "evidence_ids": [],
                "observed_signals": [],
                "missing_signals": list(stage_evidence_contract("S2").required_signals),
                "signal_bindings": {},
            }
        )
        freeze_stage_evidence(creator)
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        positive_flag = {
            "exists": True,
            "merged_with_s3": False,
            "handoff_met": True,
            "s1_s2_compatible": True,
            "product_identity_clear": True,
            "product_role_clear": True,
            "excluded_or_risky_module": False,
            "module_type": "A",
            "start_seconds": 1.0,
            "end_seconds": 2.0,
            "handoff_reason": "错误的正向判断。",
            "evidence_ids": [],
        }
        stages[1].update(
            {
                "creator_s2": positive_flag,
                "benchmark_s2": {**positive_flag, "exists": False},
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": creator,
                "benchmark": self._active_side("B"),
            },
        }

        with self.assertRaisesRegex(SystemExit, "creator_s2.evidence_ids"):
            validate_s2_contract_flags(result, {"s2_flags_required": True})

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
            "signal_bindings": self._signal_bindings("S5", "C5"),
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
        freeze_stage_evidence(side)
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
            "signal_bindings": self._signal_bindings("S5", "C5"),
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

    def test_every_stage_has_the_same_four_boundary_contracts(self) -> None:
        self.assertEqual(stage_boundary_contract_issues(), [])
        self.assertEqual(set(STAGE_BOUNDARY_TESTS), set(stage_codes()))
        for stage in stage_codes():
            self.assertEqual(
                set(STAGE_BOUNDARY_TESTS[stage]),
                {"own_positive", "not_own_negative", "previous_stage_confusion", "next_stage_confusion"},
            )

    def test_stage1_judgment_fields_fail_closed_even_when_nested(self) -> None:
        issues = stage1_forbidden_field_issues(
            {"evidence_units": [{"id": "C1", "observation": {"severity": "large"}}]}
        )
        self.assertEqual(issues, ["evidence_units[0].observation.severity"])

    def test_frozen_stage1_evidence_detects_content_and_time_mutation(self) -> None:
        side = self._active_side("C", "present")
        side["evidence_units"][0]["time_range"] = "5.0s - 6.0s"
        freeze_stage_evidence(side)
        expected = stage_evidence_sha256(side)
        self.assertEqual(side["evidence_set_version"], STAGE_EVIDENCE_SNAPSHOT_VERSION)
        self.assertEqual(stage_evidence_snapshot_issues(side), [])
        self.assertEqual(stage_evidence_immutability_issues({"video_understanding": {"creator": side}}), [])

        side["evidence_units"][0]["time_range"] = "4.0s - 6.0s"
        issues = stage_evidence_snapshot_issues(side, expected_sha256=expected)
        self.assertIn("evidence_set_sha256_mismatch", issues)
        self.assertIn("evidence_set_changed_after_lock", issues)

        content_side = self._active_side("C", "unknown")
        content_side["content_summary"] = "初始观察"
        freeze_stage_evidence(content_side)
        expected_content = stage_evidence_sha256(content_side)
        content_side["content_summary"] = "下游不应改写的观察"
        content_issues = stage_evidence_snapshot_issues(content_side, expected_sha256=expected_content)
        self.assertIn("evidence_set_sha256_mismatch", content_issues)

    def test_frozen_stage1_evidence_covers_independent_qualification_metadata(self) -> None:
        side = self._active_side("C", "present")
        side["stage1_qualification"] = {
            "source": "pipeline",
            "status": "completed",
            "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_codes": list(stage_codes()),
            "evidence_id_count": 1,
        }
        freeze_stage_evidence(side)
        expected = side["evidence_set_sha256"]
        side["stage1_qualification"]["status"] = "failed"
        self.assertIn(
            "evidence_set_changed_after_lock",
            stage_evidence_snapshot_issues(side, expected_sha256=expected),
        )

    def test_product_identity_is_preserved_and_frozen_with_stage1_observations(self) -> None:
        normalized = normalize_video_understanding(
            {
                "creator": {
                    "product_identity": {
                        "brand": "Acme",
                        "product_category": "护脚霜",
                        "functional_form": "霜剂",
                        "identity_basis": "visual",
                    }
                }
            }
        )
        side = normalized["creator"]
        self.assertEqual(side["product_identity"]["brand"], "Acme")
        freeze_stage_evidence(side)
        expected = side["evidence_set_sha256"]
        side["product_identity"]["brand"] = "Other"
        self.assertIn(
            "evidence_set_changed_after_lock",
            stage_evidence_snapshot_issues(side, expected_sha256=expected),
        )

    def test_missing_structure_events_normalize_idempotently_and_keep_frozen_hash(self) -> None:
        first = normalize_video_understanding(
            {
                "benchmark": {
                    "structure_event_checks": [
                        {
                            "module_id": "S1-A",
                            "status": "unknown",
                            "coverage": "unknown",
                            "evidence_ids": [],
                        }
                    ]
                }
            }
        )
        second = normalize_video_understanding(first)
        self.assertEqual(
            first["benchmark"]["structure_event_checks"],
            second["benchmark"]["structure_event_checks"],
        )

        side = first["benchmark"]
        freeze_stage_evidence(side)
        expected = side["evidence_set_sha256"]
        renormalized = normalize_video_understanding({"benchmark": side})["benchmark"]
        self.assertEqual(stage_evidence_sha256(renormalized), expected)
        self.assertEqual(stage_evidence_snapshot_issues(renormalized, expected_sha256=expected), [])

    def test_locked_fact_derivations_survive_trusted_normalization(self) -> None:
        side = normalize_video_understanding(
            {"creator": self._active_side("C")},
            allow_trusted_pipeline_metadata=True,
        )["creator"]
        side["evidence_budget_exceeded"] = False
        side["stage1_acquisition"] = {}
        side["stage1_qualification"] = {}
        side["stage1_coverage_audit"] = {}
        unit = side["evidence_units"][0]
        unit["trust_source_status"] = "missing"
        unit["variant_data_valid"] = False
        side["gate_observation_status"] = {
            "selling_point_route": "complete",
            "variant_focus": "complete",
            "attention_scan": "complete",
        }
        freeze_stage_evidence(side)
        expected = side["evidence_set_sha256"]

        renormalized = normalize_video_understanding(
            {"creator": side},
            trusted_stage1_acquisition={"creator": {}},
            trusted_stage1_qualification={"creator": {}},
            trusted_stage1_coverage_audit={"creator": {}},
            allow_trusted_pipeline_metadata=True,
        )["creator"]

        self.assertEqual(renormalized["evidence_units"][0]["trust_source_status"], "missing")
        self.assertFalse(renormalized["evidence_units"][0]["variant_data_valid"])
        self.assertEqual(
            renormalized["gate_observation_status"],
            side["gate_observation_status"],
        )
        self.assertEqual(stage_evidence_sha256(renormalized), expected)
        self.assertEqual(
            stage_evidence_snapshot_issues(renormalized, expected_sha256=expected),
            [],
        )

    def test_active_boundary_hint_does_not_bypass_stage_gate_with_raw_transcript(self) -> None:
        side = self._active_side("C", "present")
        block = build_s1_boundary_hint_block(
            {
                "videos": {
                    "creator": {
                        "transcript_windowed": "秘密口播不能作为第二事实源",
                    }
                }
            },
            {"creator": side},
        )
        self.assertNotIn("秘密口播不能作为第二事实源", block)
        self.assertIn("不附带原始/未资格化转写", block)

    def test_absent_stage_requires_closed_acquisition_spine(self) -> None:
        side = self._active_side("C", "unknown")
        checks = side["stage_evidence_checks"]
        checks[0] = {
            "stage": "S1",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S1").required_signals),
        }
        side["stage1_acquisition"]["speech_mode"] = "spoken"
        side["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "failed",
            "coverage": "none",
            "count": 0,
        }
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")
        self.assertIn("S1:acquisition_channel_unavailable:voiceover", stage1_acquisition_issues(side, "S1"))

    def test_sampled_visual_input_cannot_prove_absence_for_any_stage(self) -> None:
        side = self._active_side("C", "unknown")
        for check in side["stage_evidence_checks"]:
            check.update(
                {
                    "status": "absent",
                    "coverage": "complete",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": list(stage_evidence_contract(check["stage"]).required_signals),
                }
            )
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        freeze_stage_evidence(side)
        for stage in stage_codes():
            self.assertEqual(stage_evidence_readiness(side, stage), "unknown")
            self.assertIn(
                f"{stage}:acquisition_channel_coverage_incomplete:visual",
                stage1_acquisition_issues(side, stage),
            )

    def test_sampled_visual_input_can_support_direct_positive_fact(self) -> None:
        side = self._active_side("C", "present")
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S6"), "present")
        self.assertEqual(qualified_stage_evidence_ids(side, "S6"), {"C6"})

    def test_canonical_frames_are_sampled_not_full_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            work_dir = Path(root)
            frame_one = work_dir / "frame_001.jpg"
            frame_two = work_dir / "frame_002.jpg"
            audio = work_dir / "audio.wav"
            transcript = work_dir / "segments.json"
            for path in (frame_one, frame_two, audio, transcript):
                path.write_bytes(b"artifact")
            analysis = {
                "videos": {
                    "creator": {
                        "work_dir": str(work_dir),
                        "duration_seconds": 12.0,
                        "audio_path": str(audio),
                        "transcription_status": "completed",
                        "transcript_segments_path": str(transcript),
                        "speech_mode": {"mode": "visual_driven"},
                        "video_evidence": {
                            "analysis_frames": [
                                {"timestamp_seconds": 1.0, "path": str(frame_one)},
                                {"timestamp_seconds": 8.0, "path": str(frame_two)},
                            ]
                        },
                    }
                }
            }
            manifest = build_stage1_acquisition_manifest(analysis, "creator")
        self.assertEqual(manifest["channels"]["visual"]["status"], "ready")
        self.assertEqual(manifest["channels"]["visual"]["coverage"], "sampled")
        self.assertEqual(manifest["status"], "partial")

    def test_word_timestamps_without_windowed_view_do_not_claim_word_precision(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            work_dir = Path(root)
            frame = work_dir / "frame.jpg"
            audio = work_dir / "audio.wav"
            transcript = work_dir / "segments.srt"
            words = work_dir / "words.json"
            for path in (frame, audio, transcript):
                path.write_bytes(b"artifact")
            words.write_text(
                json.dumps({"words": [{"start_seconds": 0.1, "end_seconds": 0.3, "text": "hook"}]}),
                encoding="utf-8",
            )
            analysis = {
                "videos": {
                    "creator": {
                        "work_dir": str(work_dir),
                        "duration_seconds": 8.0,
                        "audio_path": str(audio),
                        "transcription_status": "completed",
                        "transcript_segments_path": str(transcript),
                        "transcript_words_path": str(words),
                        "speech_mode": {"mode": "spoken"},
                        "video_evidence": {
                            "analysis_frames": [{"timestamp_seconds": 1.0, "path": str(frame)}],
                        },
                    }
                }
            }
            manifest = build_stage1_acquisition_manifest(analysis, "creator")
        self.assertEqual(manifest["channels"]["voiceover"]["boundary_precision"], "segment")
        self.assertIn("窗口安全转写", manifest["channels"]["voiceover"]["reason"])

    def test_stage_level_voiceover_requires_word_window_precision(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage1_acquisition"]["speech_mode"] = "spoken"
        side["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "ready",
            "coverage": "full",
            "count": 1,
            "boundary_precision": "segment",
        }
        side["evidence_units"][0]["voiceover"] = "明确的开头口播"
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
            "evidence_strength": "direct",
        }
        freeze_stage_evidence(side)
        self.assertIn(
            "S1:acquisition_channel_boundary_imprecise:voiceover",
            stage1_acquisition_issues(side, "S1"),
        )
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")

    def test_stage_level_voiceover_with_word_window_precision_can_qualify(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage1_acquisition"]["speech_mode"] = "spoken"
        side["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "ready",
            "coverage": "full",
            "count": 1,
            "boundary_precision": "word",
        }
        side["evidence_units"][0]["voiceover"] = "明确的开头口播"
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
            "evidence_strength": "direct",
        }
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S1"), "present")

    def test_negative_spoken_claim_also_requires_word_window_precision(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage1_acquisition"]["speech_mode"] = "spoken"
        side["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "ready",
            "coverage": "full",
            "count": 1,
            "boundary_precision": "segment",
        }
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S1").required_signals),
        }
        freeze_stage_evidence(side)
        self.assertIn(
            "S1:acquisition_channel_boundary_imprecise:voiceover",
            stage1_acquisition_issues(side, "S1"),
        )
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")

    def test_present_stage_cannot_use_unavailable_voiceover_channel(self) -> None:
        side = self._active_side("C", "unknown")
        side["evidence_units"] = [
            {"id": "C1", "evidence_strength": "direct", "voiceover": "明确口播事实"}
        ]
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
        }
        side["stage1_acquisition"]["channels"]["voiceover"] = {
            "status": "failed",
            "coverage": "none",
            "count": 0,
        }
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")

    def test_unavailable_audio_placeholder_is_not_treated_as_audio_evidence(self) -> None:
        side = self._active_side("C", "unknown")
        side["evidence_units"][0]["audio_fact"] = (
            "当前模型未直接感知音轨，未评估语气、BGM或音效。"
        )
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C1"],
            "observed_signals": list(stage_evidence_contract("S1").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S1", "C1"),
            "evidence_strength": "direct",
        }
        freeze_stage_evidence(side)
        self.assertNotIn(
            "S1:acquisition_channel_unavailable:audio",
            stage1_acquisition_issues(side, "S1"),
        )

    def test_visual_input_timestamps_include_timeline_source_frames(self) -> None:
        self.assertEqual(
            _visual_input_timestamps(
                [
                    {
                        "timestamp_seconds": None,
                        "source_frame_timestamps": [0.0, 1.5, "invalid"],
                    },
                    {"timestamp_seconds": 4.0, "source_frame_timestamps": [1.5]},
                ]
            ),
            [0.0, 1.5, 4.0],
        )

    def test_unavailable_acquisition_also_hides_syntactically_valid_present_units(self) -> None:
        side = self._active_side("C", "present")
        side["stage_evidence_checks"][5] = {
            "stage": "S6",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C6"],
            "observed_signals": list(stage_evidence_contract("S6").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S6", "C6"),
            "evidence_strength": "direct",
        }
        side["stage1_acquisition"]["channels"]["visual"] = {
            "status": "failed",
            "coverage": "none",
            "count": 0,
        }
        freeze_stage_evidence(side)
        self.assertEqual(qualified_stage_evidence_ids(side, "S6"), set())
        view = stage_analysis_evidence_view({"creator": side})
        self.assertEqual(view["creator"]["stage_evidence_units"]["S6"], [])
        self.assertEqual(view["creator"]["stage_evidence_readiness"]["S6"], "unknown")

    def test_stage_without_direct_input_cannot_claim_absent_or_present(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage_evidence_checks"][3] = {
            "stage": "S4",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S4").required_signals),
            "signal_bindings": self._signal_bindings("S4", "", "missing"),
        }
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        side["stage1_acquisition"]["visual_input_timestamps"] = []
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S4"), "unknown")
        self.assertIn("S4:acquisition_channel_coverage_incomplete:visual", stage1_acquisition_issues(side, "S4"))

    def test_positive_visual_claim_requires_direct_stage_input(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage_evidence_checks"][3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C4"],
            "observed_signals": list(stage_evidence_contract("S4").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S4", "C4"),
        }
        side["evidence_units"][3]["visual_fact"] = "效果变化直接可见"
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        side["stage1_acquisition"]["visual_input_timestamps"] = []
        freeze_stage_evidence(side)
        self.assertIn(
            "S4:acquisition_visual_input_unobserved",
            stage1_acquisition_issues(side, "S4"),
        )
        self.assertEqual(stage_evidence_readiness(side, "S4"), "unknown")

    def test_positive_visual_claim_uses_evidence_range_not_fixed_stage_slice(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage_evidence_checks"][3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C4"],
            "observed_signals": list(stage_evidence_contract("S4").required_signals),
            "missing_signals": [],
            "signal_bindings": self._signal_bindings("S4", "C4"),
        }
        side["evidence_units"][3]["time_range"] = "3.4s - 3.8s"
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        side["stage1_acquisition"]["stage_coverage"]["S4"] = {"status": "unknown", "count": 0}
        side["stage1_acquisition"]["visual_input_timestamps"] = [3.5]
        freeze_stage_evidence(side)
        self.assertEqual(stage_evidence_readiness(side, "S4"), "present")

    def test_positive_visual_claim_is_blocked_when_sampled_input_misses_evidence_range(self) -> None:
        side = self._active_side("C", "unknown")
        side["stage_evidence_checks"][3] = {
            "stage": "S4",
            "status": "present",
            "coverage": "complete",
            "evidence_ids": ["C4"],
            "observed_signals": list(stage_evidence_contract("S4").required_signals),
            "missing_signals": [],
        }
        side["evidence_units"][3]["time_range"] = "3.4s - 3.8s"
        side["stage1_acquisition"]["channels"]["visual"]["coverage"] = "sampled"
        side["stage1_acquisition"]["visual_input_timestamps"] = [1.0]
        freeze_stage_evidence(side)
        self.assertIn(
            "S4:acquisition_visual_input_outside_evidence_range",
            stage1_acquisition_issues(side, "S4"),
        )
        self.assertEqual(stage_evidence_readiness(side, "S4"), "unknown")

    def test_explicit_visual_timestamp_must_match_an_actual_canonical_frame(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            work_dir = Path(root)
            frame_one = work_dir / "frame_001.jpg"
            frame_two = work_dir / "frame_002.jpg"
            for path in (frame_one, frame_two):
                path.write_bytes(b"artifact")
            analysis = {
                "videos": {
                    "creator": {
                        "work_dir": str(work_dir),
                        "duration_seconds": 12.0,
                        "video_evidence": {
                            "analysis_frames": [
                                {"timestamp_seconds": 1.0, "path": str(frame_one)},
                                {"timestamp_seconds": 8.0, "path": str(frame_two)},
                            ]
                        },
                    }
                }
            }
            manifest = build_stage1_acquisition_manifest(
                analysis,
                "creator",
                visual_input_timestamps=[3.5],
            )
        self.assertEqual(manifest["visual_input_timestamps"], [])
        self.assertEqual(manifest["channels"]["visual"]["coverage"], "none")
        self.assertEqual(manifest["channels"]["visual"]["status"], "failed")

    def test_failed_asr_cannot_become_visual_only_negative_claim(self) -> None:
        analysis = {
            "videos": {
                "creator": {
                    "duration_seconds": 12.0,
                    "audio_path": "/tmp/audio.wav",
                    "transcription_status": "failed",
                    "video_evidence": {
                        "analysis_frames": [
                            {"timestamp_seconds": 1.0, "path": "/tmp/frame.jpg"},
                            {"timestamp_seconds": 8.0, "path": "/tmp/frame2.jpg"},
                        ]
                    },
                }
            }
        }
        manifest = build_stage1_acquisition_manifest(analysis, "creator")
        self.assertEqual(manifest["speech_mode"], "unknown")
        side = self._active_side("C", "unknown")
        side["stage1_acquisition"] = manifest
        side["stage_evidence_checks"][0] = {
            "stage": "S1",
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(stage_evidence_contract("S1").required_signals),
        }
        freeze_stage_evidence(side)
        self.assertIn("S1:acquisition_spine_unknown_for_negative_claim", stage1_acquisition_issues(side, "S1"))

    def test_native_video_remains_visual_ready_when_frame_audit_has_warning(self) -> None:
        analysis = {
            "videos": {
                "creator": {
                    "duration_seconds": 12.0,
                    "errors": ["analysis frame manifest has a sparse gap"],
                    "video_evidence": {"errors": ["analysis frame manifest has no direct coverage for S4"]},
                }
            }
        }
        manifest = build_stage1_acquisition_manifest(
            analysis,
            "creator",
            native_video=True,
            visual_input_count=1,
        )
        self.assertEqual(manifest["channels"]["visual"]["status"], "ready")
        self.assertEqual(manifest["stage_coverage"]["S4"]["status"], "observed")

    def test_stage_links_are_explicit_and_primary_ownership_is_unique(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "present"),
                "benchmark": self._active_side("B", "present"),
            },
            "stage_evidence_links": [],
        }
        result["stage_analysis"][5]["creator_evidence_ids"] = ["C6"]
        result["stage_analysis"][5]["benchmark_evidence_ids"] = ["B6"]
        result["stage_evidence_links"] = normalize_stage_evidence_links(
            [
                {
                    "stage_id": "S6",
                    "role": "creator",
                    "evidence_id": "C6",
                    "relation": "primary",
                    "linking_reason": "结尾明确出现购买入口。",
                    "confidence": "high",
                },
                {
                    "stage_id": "S6",
                    "role": "benchmark",
                    "evidence_id": "B6",
                    "relation": "primary",
                    "linking_reason": "结尾明确出现购买入口。",
                    "confidence": "high",
                },
            ],
            result["stage_analysis"],
        )
        self.assertEqual(stage_evidence_link_issues(result), [])

        result["stage_analysis"][4]["creator_evidence_ids"] = ["C6"]
        result["stage_evidence_links"].append(
            {
                "stage_id": "S5",
                "role": "creator",
                "evidence_id": "C6",
                "relation": "primary",
                "linking_reason": "错误地把 CTA 当成信任来源。",
                "confidence": "high",
                "source": "model",
            }
        )
        issues = stage_evidence_link_issues(result)
        self.assertTrue(any("primary_ownership_conflict" in item for item in issues))
        self.assertTrue(any("unqualified_evidence_id:S5:creator:C6" in item for item in issues))

    def test_link_reconciliation_downgrades_duplicate_primary_ownership(self) -> None:
        result = {
            "stage_analysis": [
                {"stage": "S1 Hook", "creator_evidence_ids": ["C1"]},
                {"stage": "S2 产品引出", "creator_evidence_ids": ["C1"]},
            ],
            "video_understanding": {"creator": {"evidence_units": [{"id": "C1"}]}},
            "stage_evidence_links": [
                {
                    "stage_id": stage,
                    "role": "creator",
                    "evidence_id": "C1",
                    "relation": "primary",
                    "linking_reason": "模型认为该事实承担主要作用。",
                    "confidence": "high",
                    "source": "model",
                }
                for stage in ("S1", "S2")
            ],
        }
        reconcile_stage_evidence_links(result)
        self.assertEqual(
            [item["relation"] for item in result["stage_evidence_links"]],
            ["primary", "supporting"],
        )
        self.assertFalse(
            any("primary_ownership_conflict" in issue for issue in stage_evidence_link_issues(result))
        )

    def test_active_stage_link_cannot_bypass_stage_specific_qualification(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "present"),
                "benchmark": self._active_side("B", "present"),
            },
            "stage_evidence_links": [
                {
                    "stage_id": "S1",
                    "role": "creator",
                    "evidence_id": "C6",
                    "relation": "supporting",
                    "linking_reason": "跨阶段复用但未通过 S1 资格审查。",
                    "confidence": "high",
                    "source": "model",
                }
            ],
        }
        result["stage_analysis"][0]["creator_evidence_ids"] = ["C6"]
        issues = stage_evidence_link_issues(result)
        self.assertIn("link[0]:unqualified_evidence_id:S1:creator:C6", issues)

    def test_nested_stage_reference_must_be_in_top_level_stage_reference_list(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "present"),
                "benchmark": self._active_side("B", "present"),
            },
            "stage_evidence_links": [],
        }
        result["stage_analysis"][5]["creator_s6"] = {"evidence_ids": ["C6"]}
        issues = stage_evidence_link_issues(result)
        self.assertIn("S6:creator:nested_reference_missing_from_stage_list:C6", issues)

    def test_stage_link_source_is_restricted(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "present"),
                "benchmark": self._active_side("B", "present"),
            },
            "stage_evidence_links": normalize_stage_evidence_links(
                [
                    {
                        "stage_id": "S6",
                        "role": "creator",
                        "evidence_id": "C6",
                        "relation": "primary",
                        "confidence": "high",
                        "linking_reason": "明确 CTA。",
                        "source": "untrusted",
                    }
                ],
                [],
            ),
        }
        result["stage_analysis"][5]["creator_evidence_ids"] = ["C6"]
        issues = stage_evidence_link_issues(result)
        self.assertIn("link[0]:invalid_source", issues)

    def test_compatibility_links_are_visible_and_reconciliation_prunes_stale_refs(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5]["creator_evidence_ids"] = ["C6"]
        links = normalize_stage_evidence_links([], stages)
        self.assertEqual(links[0]["source"], "compatibility")
        self.assertTrue(links[0]["linking_reason"])
        result = {
            "stage_analysis": stages,
            "stage_evidence_links": links,
        }
        stages[5]["creator_evidence_ids"] = []
        reconcile_stage_evidence_links(result)
        self.assertEqual(result["stage_evidence_links"], [])

    def test_stage_evidence_gate_is_shared_by_all_stage_codes(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "video_understanding": {
                "creator": self._active_side("C", "unknown"),
                "benchmark": self._active_side("B", "unknown"),
            },
        }
        materialize_stage_evidence_gates(result)
        self.assertEqual(
            {stage["stage_evidence_gate"]["status"] for stage in result["stage_analysis"]},
            {"blocked"},
        )
        self.assertTrue(all(stage["analysis_status"] == "evidence_blocked" for stage in result["stage_analysis"]))

        for role_code, role in (("C", "creator"), ("B", "benchmark")):
            side = self._active_side(role_code, "present")
            side["evidence_units"] = [
                {
                    "id": f"{role_code}{index}",
                    "evidence_strength": "direct",
                    "visual_fact": "该阶段可直接观察的事实",
                }
                for index in range(1, 7)
            ]
            side["stage_evidence_checks"] = [
                {
                    "stage": stage,
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": [f"{role_code}{index}"],
                    "observed_signals": list(stage_evidence_contract(stage).required_signals),
                    "missing_signals": [],
                    "signal_bindings": self._signal_bindings(stage, f"{role_code}{index}"),
                }
                for index, stage in enumerate(stage_codes(), start=1)
            ]
            freeze_stage_evidence(side)
            result["video_understanding"][role] = side
        result["stage_analysis"][5]["comparison_status"] = "not_applicable"
        materialize_stage_evidence_gates(result)
        self.assertEqual(result["stage_analysis"][5]["stage_evidence_gate"]["status"], "not_applicable")
        self.assertEqual(result["stage_analysis"][0]["stage_evidence_gate"]["status"], "grounded")

    def test_blocked_stage_cannot_reach_resolver_or_publish_improvement(self) -> None:
        result = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "severity": "large",
                    "model_severity": "large",
                }
                for index in range(1, 7)
            ],
            "video_understanding": {
                "creator": self._active_side("C", "unknown"),
                "benchmark": self._active_side("B", "unknown"),
            },
            "improvements": [{"target_stage": "S4", "title": "补充效果展示"}],
        }
        materialize_stage_evidence_gates(result)
        derive_severity_from_facts(result)
        self.assertEqual(result["stage_analysis"][3]["severity_derivation"]["status"], "evidence_blocked")
        apply_comparison_eligibility(result)
        stage = result["stage_analysis"][3]
        self.assertIsNone(stage_report_severity(stage))
        self.assertTrue(stage_skipped(stage)[0])
        self.assertEqual(result["improvements"], [])

    def test_active_clamp_rejects_invalid_evidence_but_preserves_valid_range(self) -> None:
        side = self._active_side("C", "present")
        side["evidence_units"][0]["time_range"] = "5.0s - 6.0s"
        freeze_stage_evidence(side)
        result = {
            "video_understanding": {"creator": side},
            "stage_analysis": [{} for _ in range(6)],
            "improvements": [],
        }
        clamp_result_time_ranges(result, {"videos": {"creator": {"duration_seconds": 6.0}}})
        self.assertEqual(result["video_understanding"]["creator"]["evidence_units"][0]["time_range"], "5.0s - 6.0s")
        side["evidence_units"][0]["time_range"] = "5.0s - 7.0s"
        with self.assertRaisesRegex(SystemExit, "invalid time_range"):
            clamp_result_time_ranges(result, {"videos": {"creator": {"duration_seconds": 6.0}}})


if __name__ == "__main__":
    unittest.main()
