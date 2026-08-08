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
    read_stage_group_artifact,
    reusable_stage_group_response,
    stage_group_artifact_path,
)


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
                "improvements": [],
            }
        return {
            "stages": [
                {
                    "stage": code,
                    "stage_state": "completed",
                    "relation": "benchmark_better",
                    "model_gap_magnitude": "medium",
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

            def fetch_response(_args, _api_key, request_path, _response_path):
                payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
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


if __name__ == "__main__":
    unittest.main()
