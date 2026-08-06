from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.parse import normalize_video_fact_result, normalize_video_understanding
from flayr_core.llm.pipeline import (
    _mark_video_fact_coverage_audit_failed,
    _merge_video_fact_coverage_audit,
    _merge_video_fact_recovery,
    _maybe_recover_video_facts,
    detect_low_confidence_stages,
)
from flayr_core.llm.payload import _compact_comparison_facts, build_s1_boundary_hint_block
from flayr_core.postprocess.derive import _derive_one, derive_severity_from_facts
from flayr_core.postprocess.claims_my import reconcile_certification_ownership
from flayr_core.postprocess.repair_evidence import (
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
from flayr_core.postprocess.validate import validate_evidence_alignment, validate_stage_evidence_qualification
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE_EVIDENCE_SNAPSHOT_VERSION,
    STAGE1_ACQUISITION_VERSION,
    STAGE1_COVERAGE_AUDIT_VERSION,
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
    stage1_forbidden_field_issues,
    stage_codes,
    stage_evidence_contract_issues,
    stage_evidence_readiness,
    stage_evidence_diagnostics,
    stage1_acquisition_issues,
    stage1_coverage_audit_issues,
    stage_evidence_recovery_targets,
    normalize_stage_evidence_checks,
    qualified_stage_evidence_ids,
    qualified_stage_evidence_units,
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
            "independence": "separate_request_same_model",
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
            "independence": "separate_request_same_model",
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

    def test_present_qualification_uses_unit_strength_and_required_signals(self) -> None:
        checks = self._checks("present", "inferred")
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
        }
        side["stage1_coverage_audit"] = self._coverage_audit(checks)
        freeze_stage_evidence(side)
        self.assertEqual({"C1"}, qualified_stage_evidence_ids(side, "S1"))
        side["evidence_units"][0]["evidence_strength"] = "inferred"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))
        checks[0]["evidence_strength"] = "direct"
        self.assertEqual(set(), qualified_stage_evidence_ids(side, "S1"))

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
        for stage in stage_codes():
            self.assertEqual(stage_evidence_readiness(result, stage), "unknown")
            self.assertIn(f"{stage}:coverage_audit_not_completed", stage1_coverage_audit_issues(result, stage))

    def test_coverage_audit_execution_uses_registered_targets(self) -> None:
        """The live audit path must reach merge without an undefined target variable."""
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
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "model",
            "status": "partial",
            "independence": "separate_request_same_model",
            "candidate_evidence_units": [],
            "stages": {
                stage: {
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                }
                for stage in stage_codes()
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_coverage_audit_payload",
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
                    [],
                )
        self.assertEqual(result["stage1_recovery"]["status"], "coverage_audited_with_unresolved")
        self.assertEqual(result["stage1_coverage_audit"]["status"], "partial")

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
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "model",
            "status": "partial",
            "independence": "separate_request_same_model",
            "candidate_evidence_units": [],
            "stages": {
                stage: {
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "reason": {"severity": "large"} if stage == "S4" else "未确认",
                }
                for stage in stage_codes()
            },
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "flayr_core.llm.pipeline.build_video_fact_coverage_audit_payload",
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
                    [],
                )
        self.assertEqual(result["stage1_coverage_audit"]["status"], "failed")
        self.assertIn("returned downstream fields", result["stage1_recovery"]["failure_reason"])
        self.assertIn("stages.S4.reason.severity", result["stage1_recovery"]["failure_reason"])

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
            "independence": "separate_request_same_model",
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
            "independence": "separate_request_same_model",
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
            "independence": "separate_request_same_model",
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
            "independence": "separate_request_same_model",
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
                "independence": "separate_request_same_model",
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
            "stage1_acquisition": self._active_side("C")["stage1_acquisition"],
            "stage_evidence_checks": self._checks("present", "direct"),
            "evidence_units": [{"id": "C1", "evidence_strength": "direct", "visual_fact": "直接可见"}],
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
                "independence": "separate_request_same_model",
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
