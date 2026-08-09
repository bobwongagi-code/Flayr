from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.pipeline import (  # noqa: E402
    _build_stage1_to_stage2_handoff,
    _normalize_segmented_stage,
    _project_synthesis_improvements,
    _stage1_to_stage2_handoff_issues,
    run_large_model_analysis,
)
from flayr_core.llm.stage_fact_artifacts import (  # noqa: E402
    completed_stage_fact_artifact,
    failed_stage_fact_artifact,
    reusable_stage_fact_response,
)
from scripts.check_change_scope import check_scope  # noqa: E402
from scripts.verify_bitter_lesson_contract import FrozenContractError, load_spec, validate_spec  # noqa: E402


class BitterLessonContractTests(unittest.TestCase):
    def test_layer_ownership_is_unique(self) -> None:
        spec = load_spec()
        validate_spec(spec)
        self.assertEqual(
            [layer["id"] for layer in spec["layers"]],
            ["provider", "canonical", "finalizer", "report"],
        )
        self.assertEqual(
            len({layer["owner"] for layer in spec["layers"]}),
            len(spec["layers"]),
        )

    def test_frozen_spec_rejects_semantic_drift(self) -> None:
        spec = load_spec()
        spec["types"]["evidence_state"]["values"][0] = "medium"
        with self.assertRaises(FrozenContractError):
            validate_spec(spec)

        spec = load_spec()
        spec["invariants"] = spec["invariants"][:-1]
        with self.assertRaises(FrozenContractError):
            validate_spec(spec)

    def test_verification_order_is_frozen(self) -> None:
        spec = load_spec()
        self.assertEqual(
            spec["verification_order"],
            ["fixture", "offline_replay", "fake_provider", "ordinary_sample", "boundary_sample"],
        )

    def test_stage1_handoff_is_hash_bound_and_lossless(self) -> None:
        side = {
            "evidence_set_version": "evidence_snapshot_v1",
            "evidence_set_sha256": "ledger-hash",
            "evidence_units": [{"id": "C1", "visual_fact": "真实观察"}],
            "stage_evidence_checks": [
                {
                    "stage": f"S{index}",
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "signal_bindings": {},
                }
                for index in range(1, 7)
            ],
            "stage1_coverage_audit": {"version": "coverage_audit_v1", "stages": {}},
            "stage1_acquisition": {"version": "stage1_acquisition_v1", "channels": {}},
        }
        facts = {"benchmark": copy.deepcopy(side), "creator": copy.deepcopy(side)}
        analysis = {"videos": {"benchmark": {}, "creator": {}}}
        handoff = _build_stage1_to_stage2_handoff(facts, analysis)
        self.assertEqual(_stage1_to_stage2_handoff_issues(handoff, facts, analysis), [])
        handoff["roles"]["creator"]["ledger_manifest"]["units"].clear()
        self.assertIn(
            "creator:handoff_field_mismatch:ledger_manifest",
            _stage1_to_stage2_handoff_issues(handoff, facts, analysis),
        )

    def test_unknown_stage_never_becomes_publishable_severity(self) -> None:
        stage = _normalize_segmented_stage(
            {
                "stage": "S4",
                "relation": "creator_better",
                "model_gap_magnitude": "large",
                "judgment_reason": "provider supplied a conclusion",
            },
            "S4",
            {"creator": {}, "benchmark": {}},
            {"overall_status": "comparable"},
        )
        self.assertEqual(stage["stage_state"], "unknown")
        self.assertEqual(stage["model_gap_magnitude"], "uncertain")

    def test_provider_artifact_replay_requires_exact_identity(self) -> None:
        payload = {"model": "test", "messages": [{"role": "user", "content": "fixture"}]}
        artifact = completed_stage_fact_artifact(
            role="creator",
            phase="A",
            payload=payload,
            response={"evidence_units": [{"id": "C1"}]},
            model="test-model",
            api_url="https://example.test/v1",
            response_meta={"logical_request_id": "fixture-1", "completion_attempts": 1},
        )
        response, meta = reusable_stage_fact_response(
            artifact,
            role="creator",
            phase="A",
            payload=payload,
            model="test-model",
            api_url="https://example.test/v1",
        )
        self.assertEqual(response["evidence_units"][0]["id"], "C1")
        self.assertEqual(meta["logical_request_id"], "fixture-1")
        with self.assertRaises(ValueError):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="A",
                payload={**payload, "messages": [{"role": "user", "content": "changed"}]},
                model="test-model",
                api_url="https://example.test/v1",
            )

    def test_stage3_cannot_author_mechanical_fields(self) -> None:
        stage = {
            "stage": "S6",
            "model_gap_magnitude": "large",
            "gap_type": "execution",
            "creator_time_range": "20s - 25s",
            "benchmark_time_range": "18s - 22s",
            "benchmark_evidence_ids": ["B6"],
            "evidence": ["locked evidence"],
        }
        projected = _project_synthesis_improvements(
            [
                {
                    "target_stage": "S6",
                    "title": "合法 prose",
                    "suggestion": "合法建议",
                    "gap_type": "forged",
                    "creator_time_range": "0s - 999s",
                    "benchmark_evidence_ids": ["FAKE"],
                    "priority": 99,
                }
            ],
            [stage],
        )[0]
        self.assertEqual(projected["gap_type"], "execution")
        self.assertEqual(projected["creator_time_range"], "20s - 25s")
        self.assertEqual(projected["benchmark_evidence_ids"], ["B6"])
        self.assertEqual(projected["priority"], 1)

    def test_provider_artifact_keeps_retry_metadata(self) -> None:
        artifact = completed_stage_fact_artifact(
            role="benchmark",
            phase="A",
            payload={"model": "test"},
            response={"evidence_units": []},
            model="test-model",
            api_url="https://example.test/v1",
            response_meta={
                "logical_request_id": "request-1",
                "completion_attempts": 2,
                "retry_reasons": ["invalid JSON"],
                "usage": {"total_tokens": 10},
            },
        )
        self.assertEqual(artifact["response_meta"]["completion_attempts"], 2)
        self.assertEqual(artifact["response_meta"]["retry_reasons"], ["invalid JSON"])

        failed = failed_stage_fact_artifact(
            role="benchmark",
            phase="B",
            payload={"model": "test"},
            model="test-model",
            api_url="https://example.test/v1",
            error="provider timeout",
            response_meta={"logical_request_id": "request-2", "completion_attempts": 3},
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "provider timeout")
        self.assertEqual(failed["response_meta"]["completion_attempts"], 3)

    def test_default_text_entrypoint_is_rejected(self) -> None:
        args = argparse.Namespace(llm_include_images=False, llm_dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("flayr_core.llm.pipeline.read_llm_api_key", return_value=""):
                with self.assertRaisesRegex(SystemExit, "text-only LLM"):
                    run_large_model_analysis(
                        args,
                        {},
                        Path(tmp) / "analysis_input.md",
                        Path(tmp),
                    )

    def test_change_scope_rejects_unrelated_paths(self) -> None:
        spec = load_spec()
        with mock.patch("scripts.check_change_scope._changed_paths", return_value={"scripts/flayr.py"}):
            with mock.patch("scripts.check_change_scope._line_counts", return_value=(1, 0)):
                issues = check_scope(spec, "HEAD")
        self.assertTrue(any("forbidden path" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
