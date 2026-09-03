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

from flayr_core.llm import pipeline  # noqa: E402
from flayr_core.stage_evidence_contracts import (  # noqa: E402
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_evidence_contract,
)
from flayr_core.llm.stage_fact_artifacts import (  # noqa: E402
    completed_stage_fact_artifact,
    stage_fact_artifact_path,
)


class Stage1QualificationBindingNormalizationTests(unittest.TestCase):
    @staticmethod
    def _stage_check(
        stage: str,
        *,
        binding_ids: list[str] | None = None,
        status: str = "present",
        binding_status: str = "supported",
    ) -> dict:
        contract = stage_evidence_contract(stage)
        assert contract is not None
        if binding_ids is None:
            binding_ids = ["C2"]
        if status == "unknown":
            observed_signals = []
            signal_bindings = {}
            evidence_ids = []
        else:
            observed_signals = list(contract.required_signals)
            signal_bindings = {
                signal: {
                    "status": binding_status,
                    "evidence_ids": list(binding_ids),
                    "invalid_evidence_ids": [],
                    "reason": "fixture binding",
                }
                for signal in contract.required_signals
            }
            evidence_ids = ["C1"]
        return {
            "stage": stage,
            "status": status,
            "coverage": "complete" if status != "unknown" else "unknown",
            "evidence_ids": evidence_ids,
            "observed_signals": observed_signals,
            "missing_signals": [],
            "signal_bindings": signal_bindings,
            "evidence_strength": "direct" if status != "unknown" else None,
            "reason": "fixture qualification",
        }

    @staticmethod
    def _response(
        *,
        stages: tuple[str, ...] = ("S1",),
        binding_ids_by_stage: dict[str, list[str]] | None = None,
        status_by_stage: dict[str, str] | None = None,
    ) -> dict:
        binding_ids_by_stage = binding_ids_by_stage or {}
        status_by_stage = status_by_stage or {}
        return {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": [
                Stage1QualificationBindingNormalizationTests._stage_check(
                    stage,
                    binding_ids=binding_ids_by_stage.get(stage),
                    status=status_by_stage.get(stage, "present"),
                )
                for stage in stages
            ],
        }

    @staticmethod
    def _args(*, stage1_replay_from: Path | None = None) -> object:
        return type(
            "Args",
            (),
            {
                "llm_dry_run": False,
                "llm_model": "test-model",
                "judgment_model": "test-model",
                "llm_api_url": "https://example.invalid",
                "stage1_replay_from": stage1_replay_from,
                "stage1_resume_from": None,
                "provider_replay_from": None,
                "stage2_replay_from": None,
            },
        )()

    @staticmethod
    def _facts() -> dict:
        return {"evidence_units": [{"id": "C1"}, {"id": "C2"}]}

    def _run_with_response(
        self,
        response: dict,
        run_dir: Path,
        *,
        targets: list[str] | None = None,
    ) -> tuple[dict, object]:
        args = self._args()
        analysis = {"videos": {"benchmark": {}, "creator": {}}}

        def provider_call(_args, _key, _request_path, response_path, *, response_meta):
            response_meta.update(
                {
                    "logical_request_id": "fixture-request",
                    "completion_attempts": 1,
                    "retry_reasons": [],
                    "usage": {},
                }
            )
            response_path.write_text(json.dumps(response), encoding="utf-8")
            return json.dumps(response)

        with patch(
            "flayr_core.llm.pipeline.build_stage_evidence_qualification_payload",
            return_value={"fixture": "S1"},
        ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                side_effect=provider_call,
            ) as fetch:
            result = pipeline._run_stage1_qualification(
                args,
                analysis,
                run_dir,
                "",
                "creator",
                self._facts(),
                target_stages=targets or ["S1"],
            )
        return result, fetch

    def test_empty_binding_list_is_not_replaced_by_a_default_id(self) -> None:
        raw = self._response(binding_ids_by_stage={"S1": []})
        normalized, record = pipeline._normalize_stage1_qualification_bindings(
            raw,
            phase_label="Stage1-D",
        )
        signal = stage_evidence_contract("S1").required_signals[0]
        self.assertEqual(
            normalized["stage_evidence_checks"][0]["signal_bindings"][signal][
                "evidence_ids"
            ],
            [],
        )
        self.assertIsNone(record)

    def test_binding_reference_is_normalized_by_one_way_deletion(self) -> None:
        raw = self._response(binding_ids_by_stage={"S1": ["C1", "C2"]})
        original = copy.deepcopy(raw)
        normalized, record = pipeline._normalize_stage1_qualification_bindings(
            raw,
            phase_label="Stage1-D",
        )
        self.assertEqual(raw, original)
        self.assertEqual(
            normalized["stage_evidence_checks"][0]["evidence_ids"],
            ["C1"],
        )
        signal = stage_evidence_contract("S1").allowed_signals[0]
        self.assertEqual(
            normalized["stage_evidence_checks"][0]["signal_bindings"][signal][
                "evidence_ids"
            ],
            ["C1"],
        )
        self.assertEqual(record["reason_code"], pipeline.STAGE1_QUALIFICATION_BINDING_ERROR_CODE)
        self.assertEqual(record["removed_refs"][0]["removed_evidence_ids"], ["C2"])

    def test_top_level_ids_are_not_added_and_raw_artifact_is_unchanged(self) -> None:
        raw = self._response(binding_ids_by_stage={"S1": ["C1", "C2"]})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result, fetch = self._run_with_response(raw, run_dir)
            self.assertEqual(fetch.call_count, 1)
            record = result["stage1_qualification"]["group_records"][-1]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(
                record["qualification_normalization"]["removed_refs"][0][
                    "removed_evidence_ids"
                ],
                ["C2"],
            )
            signal = stage_evidence_contract("S1").allowed_signals[0]
            self.assertEqual(
                result["stage_evidence_checks"][0]["signal_bindings"][signal][
                    "evidence_ids"
                ],
                ["C1"],
            )
            artifact = json.loads(
                (run_dir / "stage1_provider_creator_D_S1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["provider_response"], raw)
            self.assertNotIn("qualification_normalization", artifact)
            self.assertFalse(
                (run_dir / "llm_facts_creator_requalification_S1_repair_response.json").exists()
            )

    def test_supported_binding_clearing_blocks_only_that_stage(self) -> None:
        raw = self._response(
            stages=("S1", "S2"),
            binding_ids_by_stage={"S1": ["C2"], "S2": ["C1"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result, fetch = self._run_with_response(
                raw,
                run_dir,
                targets=["S1", "S2"],
            )
            self.assertEqual(fetch.call_count, 1)
            record = result["stage1_qualification"]["group_records"][-1]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(
                record["qualification_normalization"]["reason_code"],
                pipeline.STAGE1_QUALIFICATION_BINDING_ERROR_CODE,
            )
            blocked = record["qualification_normalization"]["blocked_stages"]
            self.assertEqual([item["stage"] for item in blocked], ["S1"])
            self.assertEqual(result["stage1_qualification"]["status"], "failed")
            self.assertEqual(result["stage1_qualification"]["failed_stage_codes"], ["S1"])
            checks = {item["stage"]: item for item in result["stage_evidence_checks"]}
            self.assertEqual(checks["S1"]["status"], "unknown")
            self.assertEqual(checks["S2"]["status"], "present")
            artifact = json.loads(
                (run_dir / "stage1_provider_creator_D_S1_S2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["provider_response"], raw)

    def test_replay_normalizes_supported_empty_without_provider_fallback(self) -> None:
        raw = self._response(binding_ids_by_stage={"S1": ["C2"]})
        payload = {"fixture": "S1"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            run_dir = root / "run"
            source.mkdir()
            artifact = completed_stage_fact_artifact(
                role="creator",
                phase="D",
                group=["S1"],
                payload=payload,
                response=raw,
                model="test-model",
                api_url="https://example.invalid",
                response_meta={
                    "logical_request_id": "replay-fixture",
                    "completion_attempts": 1,
                    "retry_reasons": [],
                    "usage": {},
                },
            )
            stage_fact_artifact_path(source, "creator", "D", ["S1"]).write_text(
                json.dumps(artifact),
                encoding="utf-8",
            )
            args = self._args(stage1_replay_from=source)
            analysis = {"videos": {"benchmark": {}, "creator": {}}}
            with patch(
                "flayr_core.llm.pipeline.build_stage_evidence_qualification_payload",
                return_value=payload,
            ), patch(
                "flayr_core.llm.pipeline.fetch_json_completion",
                side_effect=AssertionError("strict replay must not call provider"),
            ) as fetch:
                result = pipeline._run_stage1_qualification(
                    args,
                    analysis,
                    run_dir,
                    "",
                    "creator",
                    self._facts(),
                    target_stages=["S1"],
                )

            self.assertEqual(fetch.call_count, 0)
            self.assertEqual(result["stage_evidence_checks"][0]["status"], "unknown")
            self.assertEqual(result["stage1_qualification"]["status"], "failed")
            self.assertEqual(result["stage1_qualification"]["failed_stage_codes"], ["S1"])
            group_record = result["stage1_qualification"]["group_records"][-1]
            self.assertEqual(group_record["execution_source"], "replay")
            self.assertEqual(
                group_record["qualification_normalization"]["blocked_stages"][0]["stage"],
                "S1",
            )

    def test_other_contract_errors_are_not_masked(self) -> None:
        invalid = self._response(
            binding_ids_by_stage={"S1": ["C1"]},
            status_by_stage={"S1": "not-a-status"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, fetch = self._run_with_response(invalid, Path(tmp))
            self.assertEqual(fetch.call_count, 1)
            record = result["stage1_qualification"]["group_records"][-1]
            self.assertEqual(record["status"], "failed")
            self.assertNotIn("qualification_normalization", record)


if __name__ == "__main__":
    unittest.main()
