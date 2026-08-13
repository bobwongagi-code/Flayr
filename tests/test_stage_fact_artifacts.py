from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flayr_core.llm.stage_fact_artifacts import (
    StageFactArtifactError,
    completed_stage_fact_artifact,
    failed_stage_fact_artifact,
    read_stage_fact_artifact,
    request_identity,
    reusable_stage_fact_response,
    stage_fact_artifact_path,
)
from flayr_core.llm.stage_group_artifacts import request_identity as stage_group_request_identity


def _provider_meta(request_id: str = "fixture-request") -> dict:
    return {
        "logical_request_id": request_id,
        "completion_attempts": 1,
        "retry_reasons": [],
        "usage": {},
    }


class StageFactArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "model": "qwen-test",
            "messages": [{"role": "user", "content": "locked video facts"}],
        }
        self.response = {
            "stage_evidence_contract_version": "stage_evidence_v1",
            "evidence_units": [{"id": "C1", "time_range": "0s - 1s"}],
        }

    def _artifact(self) -> dict:
        return completed_stage_fact_artifact(
            role="creator",
            phase="A",
            payload=self.payload,
            response=self.response,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            response_meta={
                "logical_request_id": "request-1",
                "completion_attempts": 2,
                "retry_reasons": ["timeout"],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
            artifact_name="stage1_provider_creator_A.json",
        )

    def test_artifact_round_trip_preserves_response_and_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = stage_fact_artifact_path(Path(tmp), "creator", "A")
            path.write_text(json.dumps(self._artifact()), encoding="utf-8")
            artifact = read_stage_fact_artifact(path)
            response, metadata = reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="A",
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )
            self.assertEqual(response, self.response)
            self.assertEqual(metadata["completion_attempts"], 2)
            self.assertEqual(path.name, "stage1_provider_creator_A.json")

    def test_payload_identity_and_response_hash_are_hard_bound(self) -> None:
        artifact = self._artifact()
        with self.assertRaisesRegex(StageFactArtifactError, "request identity mismatch"):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="A",
                payload={**self.payload, "temperature": 0.7},
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )
        artifact["provider_response"]["evidence_units"][0]["id"] = "C9"
        with self.assertRaisesRegex(StageFactArtifactError, "response hash mismatch"):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="A",
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_identity_ignores_only_run_local_paths_for_both_artifact_types(self) -> None:
        payload_a = {"messages": [{"content": [{"type": "text", "text": "本地路径：/tmp/run-a/video.mp4"}]}]}
        payload_b = {"messages": [{"content": [{"type": "text", "text": "本地路径：/tmp/run-b/video.mp4"}]}]}
        fact_a = request_identity(
            role="creator",
            phase="A",
            payload=payload_a,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )
        fact_b = request_identity(
            role="creator",
            phase="A",
            payload=payload_b,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )
        group_a = stage_group_request_identity(
            group=("S1", "S2"),
            payload=payload_a,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )
        group_b = stage_group_request_identity(
            group=("S1", "S2"),
            payload=payload_b,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )
        self.assertEqual(fact_a, fact_b)
        self.assertEqual(group_a, group_b)

    def test_failed_artifact_cannot_be_replayed(self) -> None:
        artifact = failed_stage_fact_artifact(
            role="creator",
            phase="C",
            group=["S3", "S4"],
            payload=self.payload,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            error="provider timeout",
            response_meta=_provider_meta("failed-request"),
        )
        with self.assertRaisesRegex(StageFactArtifactError, "not completed"):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="C",
                group=["S3", "S4"],
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_failed_semantic_contract_preserves_parsed_provider_response(self) -> None:
        response = {
            "stage_evidence_contract_version": "stage_evidence_v1",
            "candidate_evidence_units": [],
            "stage_evidence_checks": [],
        }
        artifact = failed_stage_fact_artifact(
            role="creator",
            phase="C",
            group=["S6"],
            payload=self.payload,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            error="Stage1-C returned phase-D fields",
            response_meta=_provider_meta("semantic-failure"),
            response=response,
        )
        self.assertEqual(artifact["provider_response"], response)
        self.assertTrue(artifact["response_sha256"])
        with self.assertRaisesRegex(StageFactArtifactError, "not completed"):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="C",
                group=["S6"],
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_failed_artifact_response_hash_is_verified_when_read(self) -> None:
        artifact = failed_stage_fact_artifact(
            role="creator",
            phase="D",
            group=["S6"],
            payload=self.payload,
            model="qwen3.7-plus",
            api_url="https://example.test/v1/chat/completions",
            error="semantic contract failure",
            response_meta=_provider_meta("failed-d"),
            response=self.response,
        )
        artifact["provider_response"]["evidence_units"][0]["id"] = "TAMPERED"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage1_provider_creator_D_S6.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(StageFactArtifactError, "response hash mismatch"):
                read_stage_fact_artifact(path)

    def test_phase_d_replay_binds_judgment_model_and_request_identity(self) -> None:
        artifact = completed_stage_fact_artifact(
            role="creator",
            phase="D",
            group=["S6"],
            payload=self.payload,
            response=self.response,
            model="qwen3.7-plus",
            api_url="https://example.test/v1/chat/completions",
            response_meta=_provider_meta("phase-d"),
            artifact_name="stage1_provider_creator_D_S6.json",
        )
        response, _metadata = reusable_stage_fact_response(
            artifact,
            role="creator",
            phase="D",
            group=["S6"],
            payload=self.payload,
            model="qwen3.7-plus",
            api_url="https://example.test/v1/chat/completions",
        )
        self.assertEqual(response, self.response)
        with self.assertRaisesRegex(StageFactArtifactError, "request identity mismatch"):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="D",
                group=["S6"],
                payload=self.payload,
                model="qwen3.6-plus",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_focused_requalification_has_distinct_phase_d_artifact(self) -> None:
        path = stage_fact_artifact_path(Path("/tmp/run"), "benchmark", "D", ["S5"])
        self.assertEqual(path.name, "stage1_provider_benchmark_D_S5.json")


if __name__ == "__main__":
    unittest.main()
