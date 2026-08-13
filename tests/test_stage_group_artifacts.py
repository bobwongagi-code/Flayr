from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from flayr_core.llm import pipeline
from flayr_core.llm.payload import STAGE_JUDGMENT_GROUPS
from flayr_core.llm.stage_group_artifacts import (
    StageGroupArtifactError,
    completed_stage_group_artifact,
    failed_stage_group_artifact,
    read_stage_group_artifact,
    revalidatable_failed_stage_group_response,
    reusable_stage_group_response,
    stage_group_artifact_path,
)


def _provider_meta(request_id: str = "fixture-request") -> dict:
    return {
        "logical_request_id": request_id,
        "completion_attempts": 1,
        "retry_reasons": [],
        "usage": {},
    }


class StageGroupArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.group = ("S1", "S2")
        self.payload = {
            "model": "qwen-test",
            "messages": [{"role": "user", "content": "locked facts"}],
        }
        self.response = {
            "stages": [
                {
                    "stage": "S1",
                    "stage_state": "completed",
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                },
                {
                    "stage": "S2",
                    "stage_state": "completed",
                    "creator_evidence_ids": ["C2"],
                    "benchmark_evidence_ids": ["B2"],
                },
            ]
        }

    def _artifact(self) -> dict:
        return completed_stage_group_artifact(
            group=self.group,
            payload=self.payload,
            response=self.response,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            response_meta=_provider_meta(),
        )

    def test_completed_response_round_trips_only_for_identical_request(self) -> None:
        restored = reusable_stage_group_response(
            self._artifact(),
            group=self.group,
            payload=self.payload,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )
        self.assertEqual(restored, self.response)

    def test_prompt_or_model_change_requires_semantic_rerun(self) -> None:
        artifact = self._artifact()
        changed_payload = dict(self.payload)
        changed_payload["messages"] = [{"role": "user", "content": "changed facts"}]
        with self.assertRaisesRegex(StageGroupArtifactError, "request identity mismatch"):
            reusable_stage_group_response(
                artifact,
                group=self.group,
                payload=changed_payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )
        with self.assertRaisesRegex(StageGroupArtifactError, "request identity mismatch"):
            reusable_stage_group_response(
                artifact,
                group=self.group,
                payload=self.payload,
                model="qwen-other",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_response_tampering_is_rejected(self) -> None:
        artifact = self._artifact()
        artifact["provider_response"]["stages"][0]["creator_evidence_ids"] = ["C9"]
        with self.assertRaisesRegex(StageGroupArtifactError, "response hash mismatch"):
            reusable_stage_group_response(
                artifact,
                group=self.group,
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_artifact_path_and_reader_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = stage_group_artifact_path(Path(tmp), self.group)
            path.write_text(json.dumps(self._artifact()), encoding="utf-8")
            self.assertEqual(path.name, "stage2_provider_S1_S2.json")
            self.assertEqual(read_stage_group_artifact(path), self._artifact())

    def test_validation_failed_response_can_be_revalidated_without_provider_call(self) -> None:
        artifact = failed_stage_group_artifact(
            group=self.group,
            payload=self.payload,
            response=self.response,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            error="local contract validation failed",
            response_meta={
                **_provider_meta(),
                "transport_status": "completed",
                "finish_reason": "stop",
                "json_valid": True,
            },
        )

        restored = revalidatable_failed_stage_group_response(
            artifact,
            group=self.group,
            payload=self.payload,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
        )

        self.assertEqual(restored, self.response)

    def test_incomplete_failed_response_cannot_be_revalidated(self) -> None:
        artifact = failed_stage_group_artifact(
            group=self.group,
            payload=self.payload,
            response=self.response,
            model="qwen-test",
            api_url="https://example.test/v1/chat/completions",
            error="truncated response",
            response_meta={
                **_provider_meta(),
                "transport_status": "completed",
                "finish_reason": "length",
                "json_valid": True,
            },
        )

        with self.assertRaisesRegex(StageGroupArtifactError, "not revalidatable"):
            revalidatable_failed_stage_group_response(
                artifact,
                group=self.group,
                payload=self.payload,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
            )

    def test_revalidation_rejects_identity_response_and_transport_tampering(self) -> None:
        def artifact(**meta_overrides) -> dict:
            return failed_stage_group_artifact(
                group=self.group,
                payload=self.payload,
                response=self.response,
                model="qwen-test",
                api_url="https://example.test/v1/chat/completions",
                error="local contract validation failed",
                response_meta={
                    **_provider_meta(),
                    "transport_status": "completed",
                    "finish_reason": "stop",
                    "json_valid": True,
                    **meta_overrides,
                },
            )

        changed_identity = artifact()
        changed_identity["request_identity"]["payload_sha256"] = "tampered"
        changed_response = artifact()
        changed_response["provider_response"]["stages"][0]["stage"] = "S6"
        failed_transport = artifact(transport_status="failed")
        invalid_json = artifact(json_valid=False)
        missing_response = artifact()
        missing_response.pop("provider_response")
        missing_response.pop("response_sha256")

        for value, reason in (
            (changed_identity, "request identity mismatch"),
            (changed_response, "response hash mismatch"),
            (failed_transport, "not revalidatable"),
            (invalid_json, "not revalidatable"),
            (missing_response, "not revalidatable"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(StageGroupArtifactError, reason):
                    revalidatable_failed_stage_group_response(
                        value,
                        group=self.group,
                        payload=self.payload,
                        model="qwen-test",
                        api_url="https://example.test/v1/chat/completions",
                    )


class StageGroupPipelineReplayTests(unittest.TestCase):
    model = "qwen-test"
    api_url = "https://example.test/v1/chat/completions"

    def _args(self, *, replay: Path | None = None, resume: Path | None = None) -> Namespace:
        return Namespace(
            llm_model=self.model,
            llm_api_url=self.api_url,
            stage2_replay_from=replay,
            stage2_resume_from=resume,
            _resource_budget=None,
        )

    @staticmethod
    def _group_payload(_model, _analysis_input, _facts, _analysis, target, **_kwargs):
        return {"kind": "stage_group", "group": list(target)}

    @staticmethod
    def _synthesis_payload(*_args, **_kwargs):
        return {"kind": "synthesis", "group": ["SYNTHESIS"]}

    @staticmethod
    def _response(group) -> dict:
        if list(group) == ["SYNTHESIS"]:
            return {
                "one_line_verdict": "replayed",
                "one_line_summary": "replayed",
                "executive_summary": "replayed",
                "holistic_assessment": {},
                "key_conclusions": ["fixture conclusion"],
                "loop_closure": {},
                "s3_s4_relationship": {},
                "promise_chain": {},
                "improvements": [],
            }
        return {
            "stages": [
                {
                    "stage": code,
                    "stage_state": "completed",
                    "relation": "benchmark_better",
                    "model_gap_magnitude": "medium",
                    "benchmark_evidence_ids": [],
                    "creator_evidence_ids": [],
                    "judgment_reason": "fixture judgment",
                }
                for code in group
            ]
        }

    def _write_completed(self, root: Path, group) -> None:
        payload = (
            self._synthesis_payload()
            if list(group) == ["SYNTHESIS"]
            else self._group_payload(None, None, None, None, group)
        )
        artifact = completed_stage_group_artifact(
            group=group,
            payload=payload,
            response=self._response(group),
            model=self.model,
            api_url=self.api_url,
            response_meta=_provider_meta(f"replay-{'-'.join(group)}"),
        )
        stage_group_artifact_path(root, group).write_text(
            json.dumps(artifact),
            encoding="utf-8",
        )

    @staticmethod
    def _project(_raw, code, _facts, _comparison=None) -> dict:
        return {
            "stage": code,
            "stage_state": "completed",
            "stage_handoff_status": "grounded",
            "model_gap_magnitude": "medium",
            "model_severity": "medium",
            "severity": "medium",
            "comparison_status": "direct",
        }

    def _run(self, args: Namespace, run_dir: Path, fetch_mock) -> dict:
        analysis = {"videos": {}, "comparison_contract": {}}
        handoff = {"version": 1, "pipeline": "segmented_stage_v1", "roles": {}}
        with (
            patch.object(pipeline, "_build_stage1_to_stage2_handoff", return_value=handoff),
            patch.object(pipeline, "_stage1_to_stage2_handoff_issues", return_value=[]),
            patch.object(pipeline, "build_stage_group_judgment_payload", side_effect=self._group_payload),
            patch.object(pipeline, "build_stage_synthesis_payload", side_effect=self._synthesis_payload),
            patch.object(pipeline, "_normalize_segmented_stage", side_effect=self._project),
            patch.object(pipeline, "fetch_json_completion", fetch_mock),
        ):
            return pipeline.run_segmented_stage_pipeline(
                args,
                analysis,
                "analysis input",
                {},
                run_dir,
                "unused-key",
            )

    def test_artifact_archive_identity_includes_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = stage_group_artifact_path(root, ("SYNTHESIS",))
            common = {
                "group": ("SYNTHESIS",), "payload": self._synthesis_payload(),
                "response": self._response(("SYNTHESIS",)), "model": self.model,
                "api_url": self.api_url,
                "response_meta": {
                    **_provider_meta("archive-identity"),
                    "transport_status": "completed", "finish_reason": "stop", "json_valid": True,
                },
            }
            first = failed_stage_group_artifact(error="first contract failure", **common)
            second = failed_stage_group_artifact(error="second contract failure", **common)
            path.write_text(json.dumps(first), encoding="utf-8")
            first_archive = pipeline._archive_stage_group_artifact(path, "validation-failed")
            path.write_text(json.dumps(second), encoding="utf-8")
            second_archive = pipeline._archive_stage_group_artifact(path, "validation-failed")

            self.assertNotEqual(first_archive, second_archive)
            self.assertEqual(
                {
                    json.loads(item.read_text(encoding="utf-8"))["error"]
                    for item in root.glob("stage2_provider_SYNTHESIS.validation-failed.*.json")
                },
                {"first contract failure", "second contract failure"},
            )

    def test_artifact_archive_rejects_preexisting_wrong_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = stage_group_artifact_path(root, ("SYNTHESIS",))
            artifact = failed_stage_group_artifact(
                group=("SYNTHESIS",), payload=self._synthesis_payload(),
                response=self._response(("SYNTHESIS",)), model=self.model,
                api_url=self.api_url, error="current failure",
                response_meta={
                    **_provider_meta("archive-corruption"),
                    "transport_status": "completed", "finish_reason": "stop", "json_valid": True,
                },
            )
            path.write_text(json.dumps(artifact), encoding="utf-8")
            digest = pipeline._stable_digest(artifact)[:12]
            collision = path.with_name(
                f"{path.stem}.validation-failed.{digest}{path.suffix}"
            )
            wrong = dict(artifact)
            wrong["error"] = "wrong preexisting content"
            collision.write_text(json.dumps(wrong), encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaisesRegex(StageGroupArtifactError, "collision or corruption"):
                pipeline._archive_stage_group_artifact(path, "validation-failed")

            self.assertEqual(path.read_bytes(), original)

    def test_strict_replay_uses_all_saved_groups_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            fetch_mock = unittest.mock.Mock(side_effect=AssertionError("provider must not be called"))

            result = self._run(self._args(replay=source), Path(run_tmp), fetch_mock)

            fetch_mock.assert_not_called()
            self.assertTrue(all(item["execution_source"] == "replay" for item in result["segmented_pipeline"]["stage_groups"]))
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")

    def test_strict_replay_rejects_identity_mismatch_without_provider_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            artifact_path = stage_group_artifact_path(source, STAGE_JUDGMENT_GROUPS[0])
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["request_identity"]["model"] = "different-model"
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            fetch_mock = unittest.mock.Mock(side_effect=AssertionError("provider must not be called"))

            result = self._run(self._args(replay=source), Path(run_tmp), fetch_mock)

            fetch_mock.assert_not_called()
            first_group = result["segmented_pipeline"]["stage_groups"][0]
            self.assertEqual(first_group["status"], "failed")
            self.assertIn("request identity mismatch", first_group["error"])
            self.assertEqual(result["stage2_candidate_status"], "degraded")

    def test_resume_calls_provider_only_for_missing_group(self) -> None:
        missing = STAGE_JUDGMENT_GROUPS[1]
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                if group != missing:
                    self._write_completed(source, group)

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                kwargs["response_meta"].update(_provider_meta("resume-provider"))
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            sources = {
                tuple(item["group"]): item["execution_source"]
                for item in result["segmented_pipeline"]["stage_groups"]
            }
            self.assertEqual(sources[missing], "provider")
            self.assertTrue(all(source_name == "replay" for group, source_name in sources.items() if group != missing))

    def test_resume_retries_identity_valid_but_semantically_invalid_group(self) -> None:
        invalid = STAGE_JUDGMENT_GROUPS[0]
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            invalid_path = stage_group_artifact_path(source, invalid)
            invalid_artifact = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_response = {
                "stages": [
                    {"stage": item["stage"]}
                    for item in invalid_artifact["provider_response"]["stages"]
                ]
            }
            invalid_artifact["provider_response"] = invalid_response
            invalid_artifact["response_sha256"] = pipeline._stable_digest(invalid_response)
            invalid_path.write_text(json.dumps(invalid_artifact), encoding="utf-8")

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                kwargs["response_meta"].update(_provider_meta("semantic-resume"))
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            first = result["segmented_pipeline"]["stage_groups"][0]
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["execution_source"], "provider")

    def test_resume_retries_duplicate_or_contradictory_stage_group(self) -> None:
        invalid = STAGE_JUDGMENT_GROUPS[0]
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            invalid_path = stage_group_artifact_path(source, invalid)
            invalid_artifact = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_response = invalid_artifact["provider_response"]
            invalid_response["stages"][0]["relation"] = "equivalent"
            invalid_response["stages"][0]["model_gap_magnitude"] = "large"
            invalid_response["stages"].append(dict(invalid_response["stages"][0]))
            invalid_artifact["response_sha256"] = pipeline._stable_digest(invalid_response)
            invalid_path.write_text(json.dumps(invalid_artifact), encoding="utf-8")

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                kwargs["response_meta"].update(_provider_meta("contract-resume"))
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            first = result["segmented_pipeline"]["stage_groups"][0]
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["execution_source"], "provider")

    def test_in_place_resume_failure_preserves_invalid_source_response(self) -> None:
        invalid = STAGE_JUDGMENT_GROUPS[0]
        with tempfile.TemporaryDirectory() as source_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            invalid_path = stage_group_artifact_path(source, invalid)
            invalid_artifact = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_response = {
                "stages": [
                    {"stage": item["stage"]}
                    for item in invalid_artifact["provider_response"]["stages"]
                ]
            }
            invalid_artifact["provider_response"] = invalid_response
            invalid_artifact["response_sha256"] = pipeline._stable_digest(invalid_response)
            invalid_path.write_text(json.dumps(invalid_artifact), encoding="utf-8")
            original = invalid_path.read_bytes()
            fetch_mock = unittest.mock.Mock(side_effect=RuntimeError("provider unavailable"))

            result = self._run(self._args(resume=source), source, fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual(invalid_path.read_bytes(), original)
            failure_path = invalid_path.with_name(
                f"{invalid_path.stem}.resume-failed{invalid_path.suffix}"
            )
            self.assertEqual(json.loads(failure_path.read_text(encoding="utf-8"))["status"], "failed")
            self.assertEqual(result["stage2_candidate_status"], "degraded")
            self.assertFalse(stage_group_artifact_path(source, ("SYNTHESIS",)).exists())
            archived = list(source.glob("stage2_provider_SYNTHESIS.superseded.*.json"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(json.loads(archived[0].read_text(encoding="utf-8"))["status"], "completed")

    def test_resume_retries_semantically_empty_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            synthesis_path = stage_group_artifact_path(source, ("SYNTHESIS",))
            synthesis_artifact = json.loads(synthesis_path.read_text(encoding="utf-8"))
            synthesis_artifact["provider_response"] = {}
            synthesis_artifact["response_sha256"] = pipeline._stable_digest({})
            synthesis_path.write_text(json.dumps(synthesis_artifact), encoding="utf-8")

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                kwargs["response_meta"].update(_provider_meta("synthesis-resume"))
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")
            recovered = json.loads(
                stage_group_artifact_path(Path(run_tmp), ("SYNTHESIS",)).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                recovered["response_meta"]["logical_request_id"],
                "synthesis-resume",
            )

    def test_resume_retries_invalid_nested_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            synthesis_path = stage_group_artifact_path(source, ("SYNTHESIS",))
            synthesis_artifact = json.loads(synthesis_path.read_text(encoding="utf-8"))
            invalid = dict(synthesis_artifact["provider_response"])
            invalid["key_conclusions"] = [{"not": "text"}]
            invalid["improvements"] = ["not-an-object"]
            synthesis_artifact["provider_response"] = invalid
            synthesis_artifact["response_sha256"] = pipeline._stable_digest(invalid)
            synthesis_path.write_text(json.dumps(synthesis_artifact), encoding="utf-8")

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                kwargs["response_meta"].update(_provider_meta("nested-synthesis-resume"))
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")

    def test_resume_revalidates_completed_synthesis_response_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as run_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            synthesis_path = stage_group_artifact_path(source, ("SYNTHESIS",))
            synthesis_artifact = json.loads(synthesis_path.read_text(encoding="utf-8"))
            response = dict(synthesis_artifact["provider_response"])
            response["improvements"] = [{
                "title": "修正钩子",
                "target_stage": "S1",
                "problem": "钩子不清楚",
                "suggestion": "强化视觉痛点",
                "actions": "增加痛点特写",
                "gmv_reason": "减少首屏流失",
                "gmv_impact": "提升停留",
            }]
            failed = failed_stage_group_artifact(
                group=("SYNTHESIS",),
                payload=self._synthesis_payload(),
                response=response,
                model=self.model,
                api_url=self.api_url,
                error="Stage3 synthesis actions type invalid",
                response_meta={
                    **_provider_meta("validation-only-failure"),
                    "transport_status": "completed",
                    "finish_reason": "stop",
                    "json_valid": True,
                },
            )
            synthesis_path.write_text(json.dumps(failed), encoding="utf-8")
            fetch_mock = unittest.mock.Mock(side_effect=AssertionError("provider must not be called"))

            result = self._run(self._args(resume=source), Path(run_tmp), fetch_mock)

            fetch_mock.assert_not_called()
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")
            self.assertEqual(
                result["segmented_pipeline"]["synthesis_execution_source"],
                "revalidation",
            )
            self.assertEqual(result["improvements"][0]["actions"], ["增加痛点特写"])
            recovered = json.loads(
                stage_group_artifact_path(Path(run_tmp), ("SYNTHESIS",)).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                recovered["provider_response"]["improvements"][0]["actions"],
                "增加痛点特写",
            )

    def test_in_place_revalidation_preserves_original_failure_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as source_tmp:
            source = Path(source_tmp)
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                self._write_completed(source, group)
            synthesis_path = stage_group_artifact_path(source, ("SYNTHESIS",))
            synthesis_artifact = json.loads(synthesis_path.read_text(encoding="utf-8"))
            response = dict(synthesis_artifact["provider_response"])
            response["improvements"] = [{
                "title": "修正钩子", "target_stage": "S1", "problem": "问题",
                "suggestion": "建议", "actions": "动作", "gmv_reason": "原因",
                "gmv_impact": "影响",
            }]
            failed = failed_stage_group_artifact(
                group=("SYNTHESIS",), payload=self._synthesis_payload(), response=response,
                model=self.model, api_url=self.api_url, error="old validation failure",
                response_meta={
                    **_provider_meta("in-place-revalidation"),
                    "transport_status": "completed", "finish_reason": "stop", "json_valid": True,
                },
            )
            synthesis_path.write_text(json.dumps(failed), encoding="utf-8")
            old_snapshot = synthesis_path.with_name(
                f"{synthesis_path.stem}.validation-failed.older.json"
            )
            old_failed = dict(failed)
            old_failed["error"] = "older validation failure"
            old_snapshot.write_text(json.dumps(old_failed), encoding="utf-8")
            fetch_mock = unittest.mock.Mock(side_effect=AssertionError("provider must not be called"))

            result = self._run(self._args(resume=source), source, fetch_mock)

            fetch_mock.assert_not_called()
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")
            preserved = list(source.glob("stage2_provider_SYNTHESIS.validation-failed.*.json"))
            self.assertEqual(len(preserved), 2)
            errors = {
                json.loads(path.read_text(encoding="utf-8"))["error"]
                for path in preserved
            }
            self.assertEqual(errors, {"older validation failure", "old validation failure"})
            self.assertEqual(json.loads(synthesis_path.read_text(encoding="utf-8"))["status"], "completed")

    def test_failed_stage_group_skips_synthesis_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            calls: list[tuple[str, ...]] = []

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                group = tuple(payload["group"])
                calls.append(group)
                if group == STAGE_JUDGMENT_GROUPS[0]:
                    raise RuntimeError("first group failed")
                kwargs["response_meta"].update(_provider_meta(f"provider-{'-'.join(group)}"))
                return json.dumps(self._response(group))

            result = self._run(self._args(), run_dir, unittest.mock.Mock(side_effect=fetch_response))

            self.assertEqual(calls, list(STAGE_JUDGMENT_GROUPS))
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "failed")
            self.assertEqual(result["segmented_pipeline"]["synthesis_execution_source"], "not_run")
            self.assertFalse(stage_group_artifact_path(run_dir, ("SYNTHESIS",)).exists())

    def test_provider_path_runs_all_groups_and_persists_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)

            def fetch_response(_args, _api_key, request_path, _response_path, **kwargs):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
                response_meta = kwargs["response_meta"]
                response_meta.update(
                    {
                        "logical_request_id": f"fake-{payload['group'][0]}",
                        "completion_attempts": 1,
                        "retry_reasons": [],
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                    }
                )
                return json.dumps(self._response(payload["group"]))

            fetch_mock = unittest.mock.Mock(side_effect=fetch_response)
            result = self._run(self._args(), run_dir, fetch_mock)

            self.assertEqual(fetch_mock.call_count, len(STAGE_JUDGMENT_GROUPS) + 1)
            self.assertEqual(result["segmented_pipeline"]["synthesis_status"], "completed")
            for group in (*STAGE_JUDGMENT_GROUPS, ("SYNTHESIS",)):
                artifact = stage_group_artifact_path(run_dir, group)
                self.assertTrue(artifact.is_file())
                value = json.loads(artifact.read_text(encoding="utf-8"))
                self.assertEqual(value["status"], "completed")
                self.assertEqual(value["response_meta"]["completion_attempts"], 1)

    def test_default_entrypoint_runs_phase_c_without_legacy_s4_hook(self) -> None:
        args = Namespace(
            llm_include_images=True,
            llm_dry_run=False,
            llm_model=self.model,
            llm_api_url=self.api_url,
            llm_api_key_env="TEST_KEY",
            llm_api_key_keychain_service=None,
            llm_api_key_keychain_account="API_KEY",
        )
        facts = {"benchmark": {}, "creator": {}}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            analysis_input = run_dir / "analysis_input.md"
            analysis_input.write_text("locked input", encoding="utf-8")
            analysis = {"videos": {}, "comparison_contract": {}}
            with (
                patch.object(pipeline, "read_llm_api_key", return_value="secret"),
                patch.object(pipeline, "load_brand_proposition", return_value=None),
                patch.object(pipeline, "establish_product_foundation", return_value=None),
                patch.object(pipeline, "run_video_fact_extraction", return_value=facts),
                patch.object(pipeline, "maybe_run_absolute_execution_shadow"),
                patch.object(pipeline, "establish_comparison_eligibility", return_value={"overall_status": "direct"}),
                patch.object(pipeline, "run_segmented_stage_pipeline", return_value={"stage2_pipeline_version": "segmented_stage_v1"}),
                patch.object(pipeline, "_process_llm_result", return_value={"stage_analysis": []}) as process,
                patch.object(pipeline, "maybe_refine_low_confidence_stages", return_value={"stage_analysis": []}) as phase_c,
                patch.object(pipeline, "maybe_reconcile_final_improvements", return_value={"stage_analysis": []}),
                patch.object(pipeline, "finalize_analysis_result", return_value={"stage2_pipeline_status": "completed"}),
            ):
                result_path, result = pipeline.run_large_model_analysis(
                    args,
                    analysis,
                    analysis_input,
                    run_dir,
                )
            self.assertEqual(result_path, run_dir / "analysis_result.json")
            self.assertEqual(result["stage2_pipeline_status"], "completed")
            process.assert_called_once()
            phase_c.assert_called_once()
            self.assertFalse(hasattr(pipeline, "maybe_apply_s4_visual_verifier"))

    def test_degraded_stage2_does_not_spend_phase_c_or_reconciliation_calls(self) -> None:
        for degraded in (
            {"stage2_candidate_status": "degraded", "stage_analysis": []},
            {
                "stage2_candidate_status": "completed",
                "stage2_pipeline_status": "degraded",
                "stage_analysis": [],
            },
        ):
            with self.subTest(degraded=degraded):
                with (
                    patch.object(pipeline, "_process_llm_result", return_value=degraded),
                    patch.object(pipeline, "maybe_refine_low_confidence_stages") as phase_c,
                    patch.object(pipeline, "maybe_reconcile_final_improvements") as reconcile,
                ):
                    result = pipeline._apply_live_postprocess_chain(
                        args=Namespace(), api_key="unused",
                        raw_result={"stage2_candidate_status": "completed"},
                        analysis_input="input", run_dir=Path("unused"), analysis={},
                        locked_video_understanding={},
                    )

                self.assertIs(result, degraded)
                phase_c.assert_not_called()
                reconcile.assert_not_called()

    def test_stage3_cannot_override_code_owned_ranges_or_evidence_ids(self) -> None:
        stage_results = [{
            "stage": "S3",
            "gap_type": "execution",
            "model_gap_magnitude": "large",
            "time_range": "标杆 10s - 12s / 达人 1s - 2s",
            "creator_time_range": "1s - 2s",
            "benchmark_time_range": "10s - 12s",
            "benchmark_evidence_ids": ["B3"],
            "benchmark_summary": "代码生成的标杆摘要",
            "evidence": ["代码生成的证据"],
            "gap": "代码生成的差距",
        }]
        result = pipeline._prepare_segmented_synthesis({
            "improvements": [{
                "target_stage": "S3",
                "title": "模型建议",
                "problem": "模型问题",
                "suggestion": "模型动作",
                "actions": ["保留"],
                "time_range": "0s - 99s",
                "benchmark_evidence_ids": ["FAKE"],
                "priority": 1,
            }],
        }, stage_results)
        improvement = result["improvements"][0]
        self.assertEqual(improvement["creator_time_range"], "1s - 2s")
        self.assertEqual(improvement["benchmark_time_range"], "10s - 12s")
        self.assertEqual(improvement["benchmark_evidence_ids"], ["B3"])
        self.assertEqual(improvement["priority"], 1)


if __name__ == "__main__":
    unittest.main()
