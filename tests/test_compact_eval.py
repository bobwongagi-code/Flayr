from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.flayr_core.llm import compact_eval
from scripts.flayr_core.llm.compact_eval import (
    COMPACT_EVAL_SCHEMA_VERSION,
    build_compact_eval_payload,
    build_severity_only_payload,
    build_visual_extraction_payload,
    compare_visual_extraction_units,
    diagnose_compact_evidence_references,
    load_frozen_compact_bundle,
    load_frozen_video_bundle,
    load_frozen_visual_bundle,
    normalize_visual_extraction_result,
    score_compact_result,
    select_frozen_video_bundle,
    summarize_visual_extraction_result,
    validate_compact_result,
    validate_severity_only_result,
    validate_visual_extraction_result,
)


STAGES = (
    "S1 Hook",
    "S2 产品引出",
    "S3 使用过程",
    "S4 效果呈现",
    "S5 信任放大",
    "S6 CTA",
)

SAMPLE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "/2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
    "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/"
    "9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/xAAUEQEAAAAAAAAAAAAA"
    "AAAAAAAA/9oACAEDAQE/AYf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AYf/xAAUEAEAAAAAAAA"
    "AAAAAAAAAAAA/9oACAEBAAY/Arf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgAD"
    "AAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAI"
    "AQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z"
)


def _write_bundle(root: Path) -> Path:
    run = root / "frozen"
    (run / "creator" / "frames").mkdir(parents=True)
    (run / "benchmark" / "frames").mkdir(parents=True)
    (run / "product_foundation.json").write_text(
        json.dumps({"foundation": {"product_profile": {"name": "test"}}}), encoding="utf-8"
    )
    (run / "comparison_eligibility.json").write_text("{}", encoding="utf-8")
    (run / "analysis_result.json").write_text(
        json.dumps(
            {
                "stage_analysis": [
                    {
                        "stage": stage,
                        "creator_time_range": f"{index - 1}s - {index}s",
                        "benchmark_time_range": f"{index - 1}s - {index}s",
                    }
                    for index, stage in enumerate(STAGES, 1)
                ]
            }
        ),
        encoding="utf-8",
    )
    for role, prefix in (("creator", "C"), ("benchmark", "B")):
        units = [
            {
                "id": f"{prefix}{index}",
                "time_range": f"{index - 1}s - {index}s",
                "information": f"{role} fact {index}",
                "functions": [f"S{index}_test"],
                "evidence_strength": "direct",
            }
            for index in range(1, 7)
        ]
        (run / f"video_facts_{role}.json").write_text(
            json.dumps({"evidence_units": units}), encoding="utf-8"
        )
        frames = []
        for index, stage in enumerate(STAGES, 1):
            frame = run / role / "frames" / f"frame_{index}.jpg"
            frame.write_bytes(SAMPLE_JPEG)
            frames.append({"stage": stage, "timestamp_seconds": index, "path": str(frame)})
        (run / role / "frames" / "stage_frames.json").write_text(json.dumps(frames), encoding="utf-8")
    return run


def _result(*, creator_id: str = "C1", benchmark_id: str = "B1") -> dict:
    return {
        "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
        "stage_judgments": [
            {
                "stage": stage,
                "severity": "small",
                "confidence": "high",
                "creator": {
                    "observation_state": "partial",
                    "evidence_ids": [creator_id if index == 1 else f"C{index}"],
                    "reason": "有锁定事实支持。",
                },
                "benchmark": {
                    "observation_state": "partial",
                    "evidence_ids": [benchmark_id if index == 1 else f"B{index}"],
                    "reason": "有锁定事实支持。",
                },
                "rationale": "双方阶段功能差距较小。",
            }
            for index, stage in enumerate(STAGES, 1)
        ],
    }


def _severity_result(*, scaffold: bool = False) -> dict:
    return {
        "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
        "stage_judgments": [
            {
                "stage": stage,
                **({"decision_basis": "固定事实显示双方在该阶段存在可审计差距。"} if scaffold else {}),
                "severity": "small",
                "confidence": "high",
            }
            for stage in STAGES
        ],
    }


def _extraction_result() -> dict:
    return {
        "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
        "creator_evidence_units": [
            {
                "id": "C1",
                "time_range": "0s - 1s",
                "information": "画面中明确出现产品。",
                "functions": ["S1"],
                "evidence_strength": "direct",
            }
        ],
        "benchmark_evidence_units": [
            {
                "id": "B1",
                "time_range": "0s - 1s",
                "information": "标杆画面中明确出现产品。",
                "functions": ["S1"],
                "evidence_strength": "explicit",
            }
        ],
    }


class CompactEvalContractTests(unittest.TestCase):
    def test_valid_result_is_accepted_and_scored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_compact_bundle(_write_bundle(Path(tmp)), include_images=False)
            result = _result()
            self.assertEqual(validate_compact_result(result, bundle), [])
            score = score_compact_result(result, {f"S{i}": "small" for i in range(1, 7)})
            self.assertEqual(score["correct_stages"], 6)
            self.assertEqual(score["accuracy"], 1.0)

    def test_unknown_evidence_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_compact_bundle(_write_bundle(Path(tmp)), include_images=False)
            errors = validate_compact_result(_result(creator_id="B1"), bundle)
            self.assertTrue(any("outside creator/S1" in error for error in errors))

    def test_none_state_cannot_hide_a_citation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_compact_bundle(_write_bundle(Path(tmp)), include_images=False)
            result = _result()
            result["stage_judgments"][0]["creator"]["observation_state"] = "none"
            errors = validate_compact_result(result, bundle)
            self.assertTrue(any("none state cannot cite evidence" in error for error in errors))

    def test_payload_is_small_contract_and_budget_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_compact_bundle(_write_bundle(Path(tmp)), include_images=False)
            payload = build_compact_eval_payload(
                "qwen3-vl-flash",
                bundle,
                output_budget=4096,
                output_budget_field="max_completion_tokens",
            )
            self.assertEqual(payload["max_completion_tokens"], 4096)
            self.assertNotIn("max_tokens", payload)
            self.assertEqual(payload["temperature"], 0.0)
            self.assertIn("六个阶段", payload["messages"][0]["content"])
            self.assertIn("不要输出", payload["messages"][0]["content"])

    def test_visual_content_is_part_of_frozen_source_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            first = load_frozen_compact_bundle(run, include_images=True)
            frame = run / "creator" / "frames" / "frame_1.jpg"
            changed = bytearray(SAMPLE_JPEG)
            changed[20] ^= 1
            frame.write_bytes(changed)
            second = load_frozen_compact_bundle(run, include_images=True)
            self.assertNotEqual(first.source_digest, second.source_digest)
            first_creator = next(item for item in first.visual_inputs if item["role"] == "creator" and item["stage"] == "S1")
            second_creator = next(item for item in second.visual_inputs if item["role"] == "creator" and item["stage"] == "S1")
            self.assertNotEqual(first_creator["sha256"], second_creator["sha256"])

    def test_judgment_only_contract_separates_severity_from_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            bundle = load_frozen_compact_bundle(run, include_images=False)
            self.assertEqual(validate_severity_only_result(_severity_result(), scaffold=False), [])
            self.assertEqual(validate_severity_only_result(_severity_result(scaffold=True), scaffold=True), [])
            invalid = _severity_result()
            invalid["stage_judgments"][0]["evidence_ids"] = ["C1"]
            self.assertTrue(any("unsupported fields" in error for error in validate_severity_only_result(invalid)))
            payload = build_severity_only_payload("qwen3.7-plus", bundle, scaffold=False, output_budget=4096)
            system = payload["messages"][0]["content"]
            self.assertIn("不要重新抽取视觉事实", system)
            self.assertIn("只保留 stage、severity、confidence", system)

    def test_visual_extraction_contract_forbids_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            bundle = load_frozen_visual_bundle(run, include_images=False)
            self.assertEqual(bundle.input_mode, "visual_frames_only")
            payload = build_visual_extraction_payload("qwen3-vl-flash", bundle, output_budget=4096)
            self.assertIn("不测 severity", payload["messages"][0]["content"])
            self.assertEqual(validate_visual_extraction_result(_extraction_result()), [])
            invalid = _extraction_result()
            invalid["creator_evidence_units"][0]["severity"] = "large"
            self.assertTrue(any("unsupported fields" in error for error in validate_visual_extraction_result(invalid)))

    def test_visual_extraction_prompt_uses_contract_unit_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_visual_bundle(_write_bundle(Path(tmp)), include_images=False)
            with patch.object(compact_eval, "EXTRACTION_MAX_UNITS", 20):
                payload = build_visual_extraction_payload("qwen3-vl-plus", bundle, output_budget=4096)
            self.assertIn("最多 20 个证据单元", payload["messages"][0]["content"])

    def test_raw_video_extraction_uses_video_blocks_without_facts_or_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            creator_video = run / "creator.mp4"
            benchmark_video = run / "benchmark.mp4"
            creator_video.write_bytes(b"creator-video")
            benchmark_video.write_bytes(b"benchmark-video")
            (run / "analysis.json").write_text(
                json.dumps(
                    {
                        "videos": {
                            "creator": {"path": str(creator_video), "duration_seconds": 10.0},
                            "benchmark": {"path": str(benchmark_video), "duration_seconds": 10.0},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(compact_eval, "video_to_data_url", return_value="data:video/mp4;base64,AAAA"):
                bundle = load_frozen_video_bundle(run)
            self.assertEqual(bundle.input_mode, "raw_video_only")
            self.assertEqual(len(bundle.video_inputs), 2)
            self.assertEqual(
                [item["duration_seconds"] for item in bundle.video_inputs],
                [10.0, 10.0],
            )
            self.assertEqual(bundle.visual_inputs, ())
            payload = build_visual_extraction_payload("qwen3-vl-plus", bundle, output_budget=4096)
            content = payload["messages"][1]["content"]
            self.assertTrue(any(item.get("type") == "video_url" for item in content))
            self.assertEqual(bundle.context["facts"], {})

    def test_video_controls_reuse_frozen_bytes_and_change_only_role_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            creator_video = run / "creator.mp4"
            benchmark_video = run / "benchmark.mp4"
            creator_video.write_bytes(b"creator-video")
            benchmark_video.write_bytes(b"benchmark-video")
            (run / "analysis.json").write_text(
                json.dumps(
                    {
                        "videos": {
                            "creator": {"path": str(creator_video), "duration_seconds": 84.0},
                            "benchmark": {"path": str(benchmark_video), "duration_seconds": 45.0},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(compact_eval, "video_to_data_url", return_value="data:video/mp4;base64,AAAA") as encode:
                bundle = load_frozen_video_bundle(run)
            self.assertEqual(encode.call_count, 2)
            creator_only = select_frozen_video_bundle(bundle, ("creator",))
            swapped = select_frozen_video_bundle(bundle, ("creator", "benchmark"))
            self.assertEqual(encode.call_count, 2)
            self.assertEqual(creator_only.video_inputs[0]["role"], "creator")
            self.assertIs(creator_only.video_inputs[0], bundle.video_inputs[1])
            self.assertEqual([item["role"] for item in swapped.video_inputs], ["creator", "benchmark"])
            self.assertNotEqual(bundle.source_digest, creator_only.source_digest)
            self.assertNotEqual(bundle.source_digest, swapped.source_digest)
            self.assertNotEqual(
                select_frozen_video_bundle(bundle, ("benchmark", "creator")).source_digest,
                swapped.source_digest,
            )
            payload = build_visual_extraction_payload("test-model", creator_only, output_budget=4096)
            system = payload["messages"][0]["content"]
            self.assertIn("只允许输出 creator_evidence_units", system)
            self.assertNotIn("benchmark_evidence_units", system)
            content = payload["messages"][1]["content"]
            self.assertEqual(sum(item.get("type") == "video_url" for item in content), 1)

    def test_video_control_cache_reuses_encoded_bytes_across_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _write_bundle(root)
            creator_video = run / "creator.mp4"
            benchmark_video = run / "benchmark.mp4"
            creator_video.write_bytes(b"creator-video")
            benchmark_video.write_bytes(b"benchmark-video")
            (run / "analysis.json").write_text(
                json.dumps(
                    {
                        "videos": {
                            "creator": {"path": str(creator_video), "duration_seconds": 10.0},
                            "benchmark": {"path": str(benchmark_video), "duration_seconds": 10.0},
                        }
                    }
                ),
                encoding="utf-8",
            )
            encoded = "data:video/mp4;base64," + base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode("ascii")
            cache_dir = root / "cache"
            with patch.object(compact_eval, "video_to_data_url", return_value=encoded) as encode:
                first = load_frozen_video_bundle(run, cache_dir=cache_dir)
            self.assertEqual(encode.call_count, 2)
            with patch.object(compact_eval, "video_to_data_url", side_effect=AssertionError("re-encoded")):
                second = load_frozen_video_bundle(run, cache_dir=cache_dir)
            self.assertEqual(
                [item["data_url_sha256"] for item in first.video_inputs],
                [item["data_url_sha256"] for item in second.video_inputs],
            )
            self.assertEqual(
                [item["data_url"] for item in first.video_inputs],
                [item["data_url"] for item in second.video_inputs],
            )

    def test_single_video_contract_rejects_unselected_role(self) -> None:
        single = {
            "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
            "creator_evidence_units": _extraction_result()["creator_evidence_units"],
        }
        self.assertEqual(validate_visual_extraction_result(single, expected_roles=("creator",)), [])
        self.assertTrue(
            any(
                "unsupported root fields" in error
                for error in validate_visual_extraction_result(_extraction_result(), expected_roles=("creator",))
            )
        )

    def test_visual_extraction_contract_rejects_time_range_after_source_duration(self) -> None:
        result = _extraction_result()
        errors = validate_visual_extraction_result(
            result,
            source_durations={"creator": 0.5, "benchmark": 10.0},
        )
        self.assertTrue(any("outside source duration" in error for error in errors))

    def test_visual_extraction_normalizes_time_without_mutating_model_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_visual_bundle(_write_bundle(Path(tmp)), include_images=False)
            result = _extraction_result()
            result["creator_evidence_units"][0]["time_range"] = "32.0-36.0s"
            normalized = normalize_visual_extraction_result(result, bundle)
            creator_unit = normalized["by_role"]["creator"][0]
            self.assertEqual(creator_unit["normalized_start_seconds"], 32.0)
            self.assertEqual(creator_unit["normalized_end_seconds"], 36.0)
            self.assertEqual(result["creator_evidence_units"][0]["time_range"], "32.0-36.0s")

    def test_visual_extraction_metrics_capture_duplicate_roles_and_s6_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _write_bundle(Path(tmp))
            creator_video = run / "creator.mp4"
            benchmark_video = run / "benchmark.mp4"
            creator_video.write_bytes(b"creator-video")
            benchmark_video.write_bytes(b"benchmark-video")
            (run / "analysis.json").write_text(
                json.dumps(
                    {
                        "videos": {
                            "creator": {"path": str(creator_video), "duration_seconds": 10.0},
                            "benchmark": {"path": str(benchmark_video), "duration_seconds": 10.0},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(compact_eval, "video_to_data_url", return_value="data:video/mp4;base64,AAAA"):
                bundle = load_frozen_video_bundle(run)
            result = _extraction_result()
            result["benchmark_evidence_units"] = [dict(item) for item in result["creator_evidence_units"]]
            result["benchmark_evidence_units"][0]["id"] = "B1"
            metrics = summarize_visual_extraction_result(result, bundle)
            self.assertEqual(metrics["cross_role_duplicate_rate"], 1.0)
            self.assertFalse(metrics["by_role"]["creator"]["s6_present"])
            self.assertEqual(metrics["by_role"]["creator"]["missing_stage_functions"], ["S2", "S3", "S4", "S5", "S6"])
            comparison = compare_visual_extraction_units(
                result["creator_evidence_units"], result["benchmark_evidence_units"]
            )
            self.assertEqual(comparison["exact_signature_jaccard"], 1.0)

    def test_temporal_diagnostic_reuses_overlap_semantics_and_marks_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = load_frozen_compact_bundle(_write_bundle(Path(tmp)), include_images=False)
            result = _result()
            result["stage_judgments"][1]["creator"]["evidence_ids"] = ["C1"]
            diagnostics = diagnose_compact_evidence_references(result, bundle)
            self.assertEqual(diagnostics["summary"]["temporal_mismatches"], 1)
            self.assertEqual(diagnostics["summary"]["function_stage_mismatches"], 1)
            self.assertEqual(diagnostics["summary"]["touching_boundaries"], 1)
            mismatch = next(
                item
                for item in diagnostics["checks"]
                if item["role"] == "creator" and item["stage"] == "S2"
            )
            self.assertEqual(mismatch["range_relation"], "touching_boundary")
            self.assertIn("evidence_temporal_mismatch", mismatch["diagnostic_flags"])
            self.assertIn("stage_function_mismatch", mismatch["diagnostic_flags"])

    def test_valid_response_is_scored_without_repair_or_production_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = load_frozen_compact_bundle(_write_bundle(root), include_images=False)
            result = _result()
            raw_response = {
                "choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]
            }

            def fake_call(api_url, api_key, payload_path, raw_path, **kwargs):
                payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
                self.assertEqual(payload["max_tokens"], 4096)
                self.assertEqual(kwargs["retries"], 0)
                self.assertEqual(kwargs["call_kind"], "compact_eval")
                self.assertFalse(kwargs["cleanup_raw"])
                raw_path.write_text(json.dumps(raw_response, ensure_ascii=False), encoding="utf-8")
                return json.dumps(raw_response, ensure_ascii=False)

            output_dir = root / "result"
            with patch.object(compact_eval, "read_llm_api_key", return_value="test-key"), patch.object(
                compact_eval, "call_llm_api", side_effect=fake_call
            ) as call:
                outcome = compact_eval.run_compact_evaluation(
                    model="test-model",
                    bundle=bundle,
                    output_dir=output_dir,
                    api_url="https://example.test/v1/chat/completions",
                    api_key_args=SimpleNamespace(),
                    output_budget=4096,
                    gt_stages={f"S{i}": "small" for i in range(1, 7)},
                )

            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["gt_score"]["correct_stages"], 6)
            self.assertFalse(outcome["promotion_eligible"])
            self.assertEqual(outcome["decision_scope"], "calibration_only")
            self.assertEqual(call.call_count, 1)
            self.assertTrue((output_dir / "compact_evaluation.json").is_file())
            self.assertFalse((output_dir / "analysis_result.json").exists())
            self.assertFalse((output_dir / "_SUCCESS.json").exists())

    def test_invalid_response_stops_at_contract_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = load_frozen_compact_bundle(_write_bundle(root), include_images=False)
            invalid = _result()
            invalid["stage_judgments"][0]["creator"]["evidence_ids"] = ["B1"]
            raw_response = {
                "choices": [{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}]
            }

            def fake_call(api_url, api_key, payload_path, raw_path, **kwargs):
                return json.dumps(raw_response, ensure_ascii=False)

            output_dir = root / "result"
            output_dir.mkdir()
            (output_dir / "compact_evaluation.json").write_text("stale", encoding="utf-8")
            with patch.object(compact_eval, "read_llm_api_key", return_value="test-key"), patch.object(
                compact_eval, "call_llm_api", side_effect=fake_call
            ):
                outcome = compact_eval.run_compact_evaluation(
                    model="test-model",
                    bundle=bundle,
                    output_dir=output_dir,
                    api_url="https://example.test/v1/chat/completions",
                    api_key_args=SimpleNamespace(),
                )

            self.assertEqual(outcome["status"], "contract_failed")
            self.assertIn("candidate_result", outcome)
            self.assertTrue((output_dir / "compact_failure.json").is_file())
            self.assertFalse((output_dir / "compact_evaluation.json").exists())
            self.assertFalse((output_dir / "analysis_result.json").exists())


if __name__ == "__main__":
    unittest.main()
