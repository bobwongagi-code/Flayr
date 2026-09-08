"""Regression coverage for Stage1 absent diagnostics and local audit gates."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.stage_evidence_contracts import (  # noqa: E402
    STAGE1_ACQUISITION_VERSION,
    STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
    STAGE1_COVERAGE_AUDIT_VERSION,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    freeze_stage_evidence,
    stage1_coverage_audit_issues,
    stage_codes,
    stage_evidence_contract,
    stage_evidence_contract_issues,
    stage_evidence_readiness,
)
from flayr_core.llm.payload import build_stage_evidence_qualification_payload  # noqa: E402


class ContractFixRegressionTests(unittest.TestCase):
    @staticmethod
    def _check(
        stage: str,
        *,
        status: str = "unknown",
        coverage: str = "unknown",
        evidence_ids: list[str] | None = None,
        observed_signals: list[str] | None = None,
        missing_signals: list[str] | None = None,
        signal_bindings: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "stage": stage,
            "status": status,
            "coverage": coverage,
            "evidence_ids": list(evidence_ids or []),
            "observed_signals": list(observed_signals or []),
            "missing_signals": list(missing_signals or []),
            "signal_bindings": dict(signal_bindings or {}),
        }

    @classmethod
    def _absent_side(cls, check: dict[str, object]) -> dict[str, object]:
        checks = [
            check if stage == check["stage"] else cls._check(stage)
            for stage in stage_codes()
        ]
        return {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [{"id": "C1", "evidence_strength": "direct"}],
        }

    @classmethod
    def _partial_side(
        cls,
        *,
        audit_status: str,
        audit_coverage: str = "complete",
        local_audit_status: str = "found",
    ) -> dict[str, object]:
        stage = "S2"
        check = cls._check(
            stage,
            status="partial",
            coverage="complete",
            evidence_ids=["C2"],
            observed_signals=["product_identity"],
            missing_signals=["problem_to_product_bridge"],
            signal_bindings={
                "product_identity": {
                    "status": "supported",
                    "evidence_ids": ["C2"],
                    "reason": "direct fixture binding",
                }
            },
        )
        checks = [check if code == stage else cls._check(code) for code in stage_codes()]
        audit_stages = {
            code: {
                "status": local_audit_status if code == stage else "unknown",
                "coverage": audit_coverage if code == stage else "unknown",
                "evidence_ids": ["C2"] if code == stage else [],
                "observed_signals": ["product_identity"] if code == stage else [],
                "missing_signals": ["problem_to_product_bridge"] if code == stage else [],
            }
            for code in stage_codes()
        }
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": checks,
            "evidence_units": [
                {
                    "id": "C2",
                    "evidence_strength": "direct",
                    "visual_fact": "产品与目标对象的直接画面事实",
                    "time_range": "0s - 1s",
                }
            ],
            "stage1_acquisition": {
                "version": STAGE1_ACQUISITION_VERSION,
                "source": "pipeline",
                "status": "complete",
                "input_mode": "native_video",
                "speech_mode": "visual_driven",
                "duration_seconds": 1.0,
                "channels": {
                    "visual": {
                        "status": "ready",
                        "coverage": "full",
                        "count": 1,
                        "boundary_precision": "frame",
                    }
                },
                "native_video_windows": [{"start_seconds": 0.0, "end_seconds": 1.0}],
                "visual_input_timestamps": [],
            },
            "stage1_coverage_audit": {
                "version": STAGE1_COVERAGE_AUDIT_VERSION,
                "source": "pipeline",
                "status": audit_status,
                "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
                "stages": audit_stages,
            },
        }
        freeze_stage_evidence(side)
        return side

    def test_absent_error_reasons_do_not_mislabel_complete_coverage(self) -> None:
        contract = stage_evidence_contract("S1")
        binding = {
            contract.required_signals[0]: {
                "status": "supported",
                "evidence_ids": [],
                "reason": "contradictory fixture binding",
            }
        }
        check = self._check(
            "S1",
            status="absent",
            coverage="complete",
            evidence_ids=["C1"],
            missing_signals=[],
            signal_bindings=binding,
        )
        issues = stage_evidence_contract_issues(self._absent_side(check))

        self.assertIn("S1:absence_with_evidence", issues)
        self.assertIn("S1:absence_with_supported_signals", issues)
        self.assertIn("S1:absence_missing_required_signals", issues)
        self.assertNotIn("S1:absence_without_complete_coverage", issues)

    def test_absent_coverage_shortfall_keeps_legacy_aggregate(self) -> None:
        contract = stage_evidence_contract("S1")
        check = self._check(
            "S1",
            status="absent",
            coverage="partial",
            missing_signals=list(contract.required_signals),
        )
        issues = stage_evidence_contract_issues(self._absent_side(check))
        self.assertIn("S1:absence_without_complete_coverage", issues)
        self.assertNotIn("S1:absence_with_evidence", issues)
        self.assertNotIn("S1:absence_with_supported_signals", issues)
        self.assertNotIn("S1:absence_missing_required_signals", issues)

    def test_complete_absent_contract_has_no_structural_issue(self) -> None:
        contract = stage_evidence_contract("S1")
        check = self._check(
            "S1",
            status="absent",
            coverage="complete",
            missing_signals=list(contract.required_signals),
        )
        issues = stage_evidence_contract_issues(self._absent_side(check))
        self.assertFalse(any(issue.startswith("S1:absence_") for issue in issues))

    def test_stage1_b_and_d_prompt_states_absent_reference_contract(self) -> None:
        for targets in (None, ["S1"]):
            with self.subTest(targets=targets):
                payload = build_stage_evidence_qualification_payload(
                    "test-model",
                    "creator",
                    {"videos": {"creator": {}}},
                    {"evidence_units": []},
                    targets,
                )
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn("Stage1-B/Stage1-D", text)
                self.assertIn("status=absent 时 evidence_ids 必须为空", text)
                self.assertIn("观察来源不等于合格的正向阶段 ID", text)
                self.assertIn("不得伪造 observed_disqualifiers", text)

    def test_global_partial_audit_does_not_block_closed_local_partial(self) -> None:
        side = self._partial_side(audit_status="partial")

        self.assertEqual(stage1_coverage_audit_issues(side, "S2"), [])
        self.assertEqual(stage_evidence_readiness(side, "S2"), "partial")
        self.assertEqual(side["stage1_coverage_audit"]["status"], "partial")

    def test_global_failed_or_local_partial_audit_still_blocks(self) -> None:
        failed = self._partial_side(audit_status="failed")
        self.assertIn("S2:coverage_audit_not_completed", stage1_coverage_audit_issues(failed, "S2"))
        self.assertEqual(stage_evidence_readiness(failed, "S2"), "unknown")

        local_partial = self._partial_side(audit_status="partial", audit_coverage="partial")
        self.assertIn(
            "S2:coverage_audit_not_completed",
            stage1_coverage_audit_issues(local_partial, "S2"),
        )
        self.assertEqual(stage_evidence_readiness(local_partial, "S2"), "unknown")

        for global_status in ("unknown",):
            with self.subTest(global_status=global_status):
                blocked = self._partial_side(audit_status=global_status)
                self.assertIn(
                    "S2:coverage_audit_not_completed",
                    stage1_coverage_audit_issues(blocked, "S2"),
                )
                self.assertEqual(stage_evidence_readiness(blocked, "S2"), "unknown")

        local_conflict = self._partial_side(
            audit_status="partial",
            audit_coverage="complete",
            local_audit_status="conflict",
        )
        local_conflict_issues = stage1_coverage_audit_issues(local_conflict, "S2")
        self.assertIn("S2:coverage_audit_disagrees_with_partial", local_conflict_issues)
        self.assertEqual(stage_evidence_readiness(local_conflict, "S2"), "unknown")

    def test_unrelated_incomplete_stage_remains_blocked_under_global_partial(self) -> None:
        side = self._partial_side(audit_status="partial")

        self.assertEqual(stage_evidence_readiness(side, "S2"), "partial")
        self.assertIn("S1:coverage_audit_not_completed", stage1_coverage_audit_issues(side, "S1"))
        self.assertEqual(stage_evidence_readiness(side, "S1"), "unknown")


if __name__ == "__main__":
    unittest.main()
