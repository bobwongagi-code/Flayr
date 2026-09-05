"""Stage evidence projection and closed-negative contract regressions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.pipeline import _materialize_stage_recovery_audit
from flayr_core.stage_evidence_contracts import (
    STAGE1_ACQUISITION_VERSION,
    STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
    STAGE1_COVERAGE_AUDIT_VERSION,
    STAGE1_PROJECTION_VERSION,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    freeze_stage_evidence,
    stage1_coverage_audit_issues,
    stage1_qualification_projection,
    stage_codes,
    stage_evidence_contract,
    stage_evidence_gate,
    stage_evidence_readiness,
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
                "partial": "found",
                "absent": "clear",
            }.get(status, "unknown")
            stages[stage] = {
                "status": audit_status,
                "coverage": (
                    str(check.get("coverage") or "partial")
                    if status == "partial"
                    else "complete" if audit_status in {"found", "clear"} else "unknown"
                ),
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
                "input_mode": "native_video",
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
                "native_video_windows": [{"start_seconds": 0.0, "end_seconds": 6.0}],
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

    def test_recovery_audit_replaces_stale_pre_recovery_state(self) -> None:
        side = self._active_side("C")
        s3 = next(item for item in side["stage_evidence_checks"] if item["stage"] == "S3")
        s3.update(
            {
                "status": "present",
                "coverage": "complete",
                "evidence_ids": ["C3"],
                "observed_signals": list(stage_evidence_contract("S3").required_signals),
                "missing_signals": [],
                "signal_bindings": self._signal_bindings("S3", "C3"),
                "evidence_strength": "direct",
            }
        )
        freeze_stage_evidence(side)
        side["stage1_coverage_audit"] = {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": "partial",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "target_stages": ["S3"],
            "stages": {
                "S3": {
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "reason": "恢复前尚未完成资格判断。",
                }
            },
            "errors": ["S3:focused_recovery_unresolved"],
        }

        projected = _materialize_stage_recovery_audit(side, ["S3"])

        self.assertEqual(projected["status"], "completed")
        self.assertEqual(projected["stages"]["S3"]["status"], "found")
        self.assertEqual(projected["stages"]["S3"]["coverage"], "complete")
        self.assertNotIn("S3:focused_recovery_unresolved", projected["errors"])

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


if __name__ == "__main__":
    unittest.main()
