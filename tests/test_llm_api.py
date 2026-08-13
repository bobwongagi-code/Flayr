"""Focused contracts for provider capability lookup and incremental SSE parsing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.api import (  # noqa: E402
    IncrementalSSEParser,
    can_analyze_native_audio,
    can_analyze_native_video,
    can_send_standalone_audio,
    call_llm_api,
    increase_output_budget,
    is_retryable_error,
    parse_curl_http_status,
    provider_capabilities,
    reject_retired_model,
    strip_curl_http_status,
)
from flayr_core.llm.media import build_evidence_sensory_inputs  # noqa: E402
from flayr_core.llm.payload import build_video_fact_payload  # noqa: E402
from flayr_core.llm.pipeline import (  # noqa: E402
    _deterministic_product_visibility,
    run_video_fact_extraction,
)
from flayr_core.resources import ResourceBudget, ResourceLimits  # noqa: E402
from flayr_core.llm.parse import (  # noqa: E402
    adapt_misnested_analysis_result,
    normalize_attention_competitors,
    normalize_attention_scan_audit,
    normalize_comparison_contract,
    normalize_evidence,
    normalize_fact_evidence_checklist,
    normalize_hook_flags,
    normalize_loop_closure,
    normalize_presentation_overlays,
    normalize_product_coverage,
    normalize_ratio,
    normalize_selling_point_observations,
    normalize_stage_evidence_contract_version,
    normalize_support_status,
    normalize_structure_event_checks,
    normalize_task_completion,
    normalize_variant_decision_rule,
    normalize_variant_unit_fields,
)
from flayr_core.llm.stage_fact_artifacts import StageFactArtifactError  # noqa: E402


class LlmApiContractTests(unittest.TestCase):
    def test_retired_vl_flash_models_are_rejected(self) -> None:
        for model in ("qwen3-vl-flash", "qwen3-vl-flash-2026-01-01"):
            with self.subTest(model=model), self.assertRaisesRegex(SystemExit, "model has been retired"):
                reject_retired_model(model)

        reject_retired_model("qwen3-vl-plus")

    def test_llm_transport_preserves_finalization_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            budget = mock.Mock()
            budget.limits.max_single_request_bytes = 1024 * 1024
            budget.remaining_wall_seconds.return_value = 20.0
            response_meta: dict[str, object] = {}

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command") as run,
                self.assertRaisesRegex(SystemExit, "preserving 30s for deterministic finalization"),
            ):
                call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    budget=budget,
                    response_meta=response_meta,
                )

            run.assert_not_called()
            budget.reserve_api_call.assert_not_called()
            self.assertEqual(response_meta["transport_attempts"], 0)
            self.assertEqual(response_meta["transport_status"], "failed")

    def test_llm_transport_clamps_curl_and_wrapper_before_finalization_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            budget = mock.Mock()
            budget.limits.max_single_request_bytes = 1024 * 1024
            budget.remaining_wall_seconds.return_value = 100.0
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_run(command: list[str], **kwargs: object) -> mock.Mock:
                calls.append((command, kwargs))
                callback = kwargs["stdout_callback"]
                assert callable(callback)
                callback(
                    b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n'
                )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run),
            ):
                call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    budget=budget,
                )

            self.assertEqual(len(calls), 1)
            command, options = calls[0]
            self.assertEqual(command[command.index("--max-time") + 1], "61")
            self.assertEqual(options["timeout_seconds"], 66)
            budget.reserve_api_call.assert_called_once()
            budget.reserve_download.assert_called_once()

    def test_llm_transport_retry_accounts_each_actual_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            budget = ResourceBudget(ResourceLimits(max_total_wall_time=120.0))
            calls = 0

            def fake_run(_command: list[str], **kwargs: object) -> mock.Mock:
                nonlocal calls
                calls += 1
                callback = kwargs["stdout_callback"]
                assert callable(callback)
                if calls == 1:
                    callback(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
                else:
                    callback(
                        b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                        b'data: [DONE]\n\n'
                    )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run),
                mock.patch("flayr_core.llm.api.time.sleep"),
            ):
                call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    retries=1,
                    budget=budget,
                )

            self.assertEqual(calls, 2)
            self.assertEqual(budget.llm_calls, 2)
            self.assertEqual(len(budget.api_events), 2)
            self.assertEqual([event["attempt"] for event in budget.api_events], [1, 2])
            self.assertEqual(len({event["request_id"] for event in budget.api_events}), 1)
            self.assertEqual(
                budget.total_uploaded_bytes,
                sum(event["request_bytes"] for event in budget.api_events),
            )

    def test_transport_retry_does_not_consume_output_expansion_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            calls = 0
            response_meta: dict[str, object] = {}

            def fake_run(_command: list[str], **kwargs: object) -> mock.Mock:
                nonlocal calls
                calls += 1
                callback = kwargs["stdout_callback"]
                assert callable(callback)
                if calls == 1:
                    return mock.Mock(returncode=28, stderr="curl: (28) timed out", stdout="")
                if calls in {2, 3}:
                    callback(
                        b'data: {"choices":[{"delta":{"content":"partial"},'
                        b'"finish_reason":"length"}]}\n\n'
                        b'data: [DONE]\n\n'
                    )
                else:
                    callback(
                        b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                        b'data: [DONE]\n\n'
                    )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run),
                mock.patch("flayr_core.llm.api.time.sleep"),
            ):
                raw = call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    retries=1,
                    output_expansions=2,
                    response_meta=response_meta,
                )

            self.assertEqual(calls, 4)
            self.assertIn('"finish_reason": "stop"', raw)
            self.assertEqual(
                response_meta["request_retry_kinds"],
                ["transport", "output_expansion", "output_expansion"],
            )

    def test_common_curl_connect_failures_are_retryable(self) -> None:
        self.assertTrue(is_retryable_error("curl: (7) Failed to connect to host port 443"))
        self.assertTrue(is_retryable_error("curl: (7) Couldn't connect to server"))
        self.assertTrue(is_retryable_error("curl: (7) Could not connect to server"))

    def test_no_retry_records_no_retry_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            response_meta: dict[str, object] = {}

            def fake_run(_command: list[str], **kwargs: object) -> mock.Mock:
                kwargs["stdout_callback"](
                    b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run),
                self.assertRaisesRegex(SystemExit, "流式响应不完整"),
            ):
                call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    retries=0,
                    output_expansions=0,
                    response_meta=response_meta,
                )
            self.assertEqual(response_meta["transport_attempts"], 1)
            self.assertEqual(response_meta["request_retry_kinds"], [])

    def test_budget_blocked_retry_records_no_retry_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text('{"model":"qwen3-vl-plus","messages":[]}', encoding="utf-8")
            budget = mock.Mock()
            budget.limits.max_single_request_bytes = 1024 * 1024
            budget.remaining_wall_seconds.side_effect = [100.0, 30.0]
            response_meta: dict[str, object] = {}

            def fake_run(_command: list[str], **kwargs: object) -> mock.Mock:
                kwargs["stdout_callback"](
                    b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run) as run,
                mock.patch("flayr_core.llm.api.time.sleep") as sleep,
                self.assertRaisesRegex(SystemExit, "流式响应不完整"),
            ):
                call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    retries=1,
                    output_expansions=0,
                    budget=budget,
                    response_meta=response_meta,
                )
            self.assertEqual(run.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(response_meta["request_retry_kinds"], [])

    def test_stage1_resume_falls_back_to_provider_when_a_artifact_is_missing(self) -> None:
        args = Namespace(
            llm_image_limit=8,
            llm_dry_run=False,
            llm_model="qwen-test",
            llm_api_url="https://example.test/v1/chat/completions",
            stage1_replay_from=None,
            stage1_resume_from=None,
            _resource_budget=None,
        )
        analysis = {"videos": {"creator": {}}}
        provider_response = "{}"

        def provider_call(*_args, **kwargs):
            kwargs["response_meta"].update(
                {
                    "logical_request_id": "stage1-resume-provider",
                    "transport_attempts": 1,
                    "transport_retry_reasons": [],
                    "usage": {},
                }
            )
            return provider_response

        with tempfile.TemporaryDirectory() as tmp:
            args.stage1_resume_from = Path(tmp) / "missing-stage1"
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with (
                mock.patch(
                    "flayr_core.llm.pipeline.select_role_visual_inputs",
                    return_value=[],
                ),
                mock.patch(
                    "flayr_core.llm.pipeline.build_video_fact_payload",
                    return_value={"messages": []},
                ),
                mock.patch(
                    "flayr_core.llm.pipeline.fetch_json_completion",
                    side_effect=provider_call,
                ) as fetch,
                mock.patch(
                    "flayr_core.llm.pipeline.normalize_video_fact_result",
                    return_value={"evidence_units": []},
                ),
                mock.patch(
                    "flayr_core.llm.pipeline.build_stage1_acquisition_manifest",
                    return_value={"provider_artifacts": []},
                ),
                mock.patch(
                    "flayr_core.llm.pipeline._run_stage1_qualification",
                    side_effect=lambda _args, _analysis, _run_dir, _key, _role, facts: facts,
                ),
                mock.patch(
                    "flayr_core.llm.pipeline._maybe_recover_video_facts",
                    side_effect=lambda _args, _analysis, _run_dir, _key, _role, facts: facts,
                ),
                mock.patch("flayr_core.llm.pipeline.freeze_stage_evidence"),
            ):
                result = run_video_fact_extraction(args, analysis, run_dir, "secret")
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs["request_max_time_seconds"], 300)
            self.assertEqual(fetch.call_args.kwargs["request_retries"], 1)
            self.assertIn("creator", result)
            artifact = json.loads(
                (run_dir / "stage1_provider_creator_A.json").read_text(encoding="utf-8")
            )
            self.assertEqual(artifact["status"], "completed")

    def test_stage1_strict_replay_does_not_fall_back_to_provider(self) -> None:
        args = Namespace(
            llm_image_limit=8,
            llm_dry_run=False,
            llm_model="qwen-test",
            llm_api_url="https://example.test/v1/chat/completions",
            stage1_replay_from=None,
            stage1_resume_from=None,
            _resource_budget=None,
        )
        analysis = {"videos": {"creator": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            args.stage1_replay_from = Path(tmp) / "missing-stage1"
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            with (
                mock.patch(
                    "flayr_core.llm.pipeline.select_role_visual_inputs",
                    return_value=[],
                ),
                mock.patch(
                    "flayr_core.llm.pipeline.build_video_fact_payload",
                    return_value={"messages": []},
                ),
                mock.patch(
                    "flayr_core.llm.pipeline.fetch_json_completion",
                ) as fetch,
            ):
                with self.assertRaises(StageFactArtifactError):
                    run_video_fact_extraction(args, analysis, run_dir, "secret")

        fetch.assert_not_called()

    def test_evidence_normalization_is_lossless_unless_limit_is_explicit(self) -> None:
        values = [f"E{index}" for index in range(1, 9)]

        self.assertEqual(normalize_evidence(values), values)
        self.assertEqual(normalize_evidence(values, max_items=4), values[:4])

    def test_missing_observations_stay_unknown_and_lists_are_not_silently_truncated(self) -> None:
        selling_points = [
            {"id": f"SP{index}", "text": f"point-{index}"}
            for index in range(1, 8)
        ]
        competitors = [
            {"id": f"AC{index}", "object_label": f"object-{index}"}
            for index in range(1, 8)
        ]

        self.assertIsNone(normalize_product_coverage(None))
        self.assertEqual(normalize_product_coverage("none"), "none")
        self.assertEqual(
            normalize_structure_event_checks({}, {"B1"})[0]["status"],
            "unknown",
        )
        self.assertIsNone(
            normalize_structure_event_checks(
                [{"module_id": "S1-A"}], {"B1"}
            )[0]["present"]
        )
        self.assertIsNone(normalize_attention_scan_audit({}, set())["recording_equipment_visible"])
        self.assertIsNone(normalize_variant_decision_rule({}, set())["speech_explains_choice"])
        self.assertIsNone(normalize_fact_evidence_checklist([{"item": "x"}], set())[0]["covered"])
        self.assertEqual(len(normalize_selling_point_observations(selling_points, set())), 7)
        self.assertEqual(len(normalize_attention_competitors(competitors, set())), 7)
        self.assertIsNone(normalize_hook_flags({"dims": {}})["dims"]["camera"])
        self.assertEqual(normalize_support_status(None, "a quote"), "unknown")
        self.assertIsNone(normalize_task_completion(None))
        self.assertEqual(normalize_presentation_overlays(None), ["unknown"])
        self.assertEqual(normalize_presentation_overlays([]), ["none"])
        self.assertEqual(normalize_presentation_overlays(["closeup", "not-a-real-overlay"]), ["unknown"])
        self.assertIsNone(normalize_ratio(True))
        self.assertIsNone(normalize_stage_evidence_contract_version(True))
        self.assertIsNone(normalize_loop_closure({})["pain_resolved_in_s4"])
        adapted = adapt_misnested_analysis_result({"stage_analysis": [], "product_visibility": {}})
        self.assertIsNone(adapted["product_visibility"]["ratio"])
        self.assertIsNone(adapted["loop_closure"]["suspense_revealed"])

    def test_missing_ratios_and_variant_shares_stay_unknown(self) -> None:
        self.assertIsNone(normalize_ratio(None))
        self.assertIsNone(normalize_ratio("not-a-ratio"))
        normalized = normalize_variant_unit_fields(
            {
                "variant_ids": ["black"],
                "variant_visual_shares": {"black": None},
                "variant_speech_shares": {},
                "variant_relation_mode": "single_focus",
                "comparison_purpose_explicit": None,
            }
        )
        self.assertEqual(normalized["variant_visual_shares"], {"black": None})
        self.assertFalse(normalized["variant_attribution_confident"])
        self.assertFalse(normalized["variant_data_valid"])

    def test_product_visibility_does_not_invent_zeroes_without_explicit_facts(self) -> None:
        unknown = _deterministic_product_visibility(
            {"creator": {"evidence_units": [{"id": "C1", "time_range": "0s - 1s"}]}},
            {"videos": {"creator": {"duration_seconds": 10.0}}},
        )
        self.assertIsNone(unknown["first_appearance_sec"])
        self.assertIsNone(unknown["ratio"])

        absent = _deterministic_product_visibility(
            {"creator": {"evidence_units": [{"id": "C1", "product_visible": False, "time_range": "0s - 1s"}]}},
            {"videos": {"creator": {"duration_seconds": 10.0}}},
        )
        self.assertEqual(absent["first_appearance_sec"], 0.0)
        self.assertEqual(absent["ratio"], 0.0)

    def test_missing_shared_job_facts_do_not_qualify_as_strong_substitutes(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {},
            }
        )

        self.assertEqual(contract["substitution_relation"], "partial_substitute")
        self.assertIsNone(contract["shared_job"]["same_consumer_job"])

    def test_fact_extraction_uses_full_per_request_image_limit_for_each_role(self) -> None:
        args = mock.Mock(
            llm_image_limit=12,
            llm_dry_run=True,
            llm_model="qwen3.6-plus",
            llm_api_url="https://example.test/chat/completions",
        )
        analysis = {"videos": {"benchmark": {}, "creator": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch(
                    "flayr_core.llm.pipeline.select_role_visual_inputs",
                    return_value=[],
                ) as selector,
                mock.patch(
                    "flayr_core.llm.pipeline.build_video_fact_payload",
                    return_value={"messages": []},
                ),
            ):
                run_video_fact_extraction(args, analysis, Path(tmp), "unused")
        self.assertEqual(
            [call.args[2] for call in selector.call_args_list],
            [12, 12],
        )

    def test_known_provider_capabilities_are_explicit(self) -> None:
        qwen_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        qwen = provider_capabilities(qwen_url, "qwen3-omni-flash")
        self.assertEqual(qwen.profile, "dashscope_qwen_compatible")
        self.assertEqual(qwen.confidence, "verified_matrix")
        self.assertTrue(can_send_standalone_audio(qwen_url, "qwen3-omni-flash"))
        self.assertTrue(can_analyze_native_audio(qwen_url, "qwen3-omni-flash"))
        self.assertTrue(can_analyze_native_video(qwen_url, "qwen3-omni-flash"))
        self.assertTrue(can_analyze_native_video(qwen_url, "qwen3-vl-plus"))
        self.assertFalse(can_send_standalone_audio(qwen_url, "qwen3-vl-plus"))
        self.assertFalse(can_analyze_native_audio(qwen_url, "qwen3-vl-plus"))

    def test_beijing_maas_qwen_capabilities_are_explicit(self) -> None:
        qwen_url = "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        qwen = provider_capabilities(qwen_url, "qwen3.6-plus")
        self.assertEqual(qwen.profile, "beijing_maas_qwen_transcript_visual")
        self.assertEqual(qwen.confidence, "runtime_verified")
        self.assertFalse(can_send_standalone_audio(qwen_url, "qwen3.6-plus"))
        self.assertFalse(can_analyze_native_audio(qwen_url, "qwen3.6-plus"))
        self.assertFalse(can_analyze_native_video(qwen_url, "qwen3.6-plus"))

        vision = provider_capabilities(qwen_url, "qwen3-vl-plus")
        self.assertTrue(vision.native_video_input)
        self.assertFalse(vision.standalone_audio_input)
        self.assertFalse(vision.native_audio_analysis)
        self.assertTrue(can_analyze_native_video(qwen_url, "qwen3-vl-plus"))

    def test_maas_comparison_omits_unsupported_input_audio(self) -> None:
        qwen_url = "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = {"videos": {}}
            facts = {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}}
            for role, code in (("benchmark", "B1"), ("creator", "C1")):
                role_dir = root / role
                role_dir.mkdir()
                frame = role_dir / "frame.jpg"
                frame.write_bytes(b"jpeg")
                (role_dir / "audio.wav").write_bytes(b"wav")
                analysis["videos"][role] = {
                    "work_dir": str(role_dir),
                    "duration_seconds": 2.0,
                    "frames": [],
                }
                facts[role]["evidence_units"] = [{"id": code, "time_range": "0s - 1s"}]
            with (
                mock.patch(
                    "flayr_core.llm.media.select_frames_for_time_range",
                    return_value=[{"path": str(root / "benchmark" / "frame.jpg")}],
                ),
                mock.patch(
                    "flayr_core.llm.media.image_to_data_url",
                    return_value="data:image/jpeg;base64,AA==",
                ),
                mock.patch(
                    "flayr_core.llm.media.audio_to_mp3_data_url",
                    return_value="data:audio/mp3;base64,AA==",
                ) as audio_encoder,
            ):
                content = build_evidence_sensory_inputs(
                    analysis,
                    facts,
                    api_url=qwen_url,
                    model="qwen3.6-plus",
                )
            types = [item.get("type") for item in content]
            self.assertIn("image_url", types)
            self.assertNotIn("input_audio", types)
            audio_encoder.assert_not_called()

    def test_maas_fact_payload_uses_frames_and_local_transcript_path(self) -> None:
        qwen_url = "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            frame = root / "frame.jpg"
            video.write_bytes(b"mp4")
            frame.write_bytes(b"jpeg")
            (root / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nRAW_SRT_FACT_PAYLOAD\n",
                encoding="utf-8",
            )
            (root / "transcript.words.json").write_text(
                '{"text":"RAW_WORD_FACT_PAYLOAD"}', encoding="utf-8"
            )
            (root / "transcript_windowed.md").write_text(
                "[0.0-1.0] SAFE_FACT_WINDOW", encoding="utf-8"
            )
            analysis = {
                "product": {"name": "测试产品"},
                "videos": {
                    "creator": {
                        "path": str(video),
                        "work_dir": str(root),
                        "duration_seconds": 2.0,
                        "video_evidence": {
                            "transcript_windowed_path": str(root / "transcript_windowed.md"),
                        },
                    }
                },
            }
            with mock.patch(
                "flayr_core.llm.payload.video_to_data_url",
                return_value="data:video/mp4;base64,AA==",
            ) as video_encoder:
                payload = build_video_fact_payload(
                    "qwen3.6-plus",
                    "creator",
                    analysis,
                    [{"label": "creator frame", "path": str(frame), "data_url": "data:image/jpeg;base64,AA=="}],
                    api_url=qwen_url,
                )
            content = payload["messages"][1]["content"]
            types = [item.get("type") for item in content if isinstance(item, dict)]
            self.assertNotIn("video_url", types)
            self.assertNotIn("input_audio", types)
            self.assertIn("image_url", types)
            payload_text = json.dumps(payload, ensure_ascii=False)
            self.assertIn("SAFE_FACT_WINDOW", payload_text)
            self.assertIn("Stage1-A 原子事实合同", payload_text)
            self.assertIn("不要输出 stage_evidence_checks", payload_text)
            self.assertNotIn("只有 coverage=complete 时才允许写 present 或 absent", payload_text)
            self.assertNotIn("status=unknown/conflict 时 evidence_ids 必须为空", payload_text)
            self.assertNotIn("RAW_SRT_FACT_PAYLOAD", payload_text)
            self.assertNotIn("RAW_WORD_FACT_PAYLOAD", payload_text)
            video_encoder.assert_not_called()

    def test_native_capable_provider_stage1_a_never_sends_full_video(self) -> None:
        qwen_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "video.mp4"
            frame = root / "frame.jpg"
            video.write_bytes(b"mp4")
            frame.write_bytes(b"jpeg")
            analysis = {
                "product": {"name": "测试产品"},
                "videos": {
                    "creator": {
                        "path": str(video),
                        "work_dir": str(root),
                        "duration_seconds": 2.0,
                    }
                },
            }
            with (
                mock.patch(
                    "flayr_core.llm.payload.video_to_data_url",
                    return_value="data:video/mp4;base64,AA==",
                ) as video_encoder,
                mock.patch(
                    "flayr_core.llm.payload.audio_to_mp3_data_url",
                    return_value=None,
                ),
            ):
                payload = build_video_fact_payload(
                    "qwen3-omni-flash",
                    "creator",
                    analysis,
                    [{"label": "creator frame", "path": str(frame), "data_url": "data:image/jpeg;base64,AA=="}],
                    api_url=qwen_url,
                )
            types = [
                item.get("type")
                for item in payload["messages"][1]["content"]
                if isinstance(item, dict)
            ]
            self.assertIn("image_url", types)
            self.assertNotIn("video_url", types)
            video_encoder.assert_not_called()

    def test_unknown_provider_is_conservative(self) -> None:
        capabilities = provider_capabilities("https://example.test/v1/chat/completions", "vision-test")
        self.assertEqual(capabilities.profile, "unknown_provider")
        self.assertEqual(capabilities.confidence, "unverified")
        self.assertFalse(can_send_standalone_audio("https://example.test/v1/chat/completions", "vision-test"))
        self.assertFalse(can_analyze_native_video("https://example.test/v1/chat/completions", "vision-test"))
        self.assertFalse(can_analyze_native_audio("https://example.test/v1/chat/completions", "vision-test"))

    def test_length_at_output_cap_is_returned_once_for_outer_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text(
                '{"model":"qwen3.6-plus","max_completion_tokens":65536,"messages":[]}',
                encoding="utf-8",
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                kwargs["stdout_callback"](
                    b'data: {"choices":[{"delta":{"content":"{\\"partial\\":true"},'
                    b'"finish_reason":"length"}] }\n\n'
                    b'data: [DONE]\n\n'
                )
                return mock.Mock(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch("flayr_core.llm.api.validate_outbound_url", return_value=mock.Mock(
                    hostname="example.test", port=443, resolved_addresses=("203.0.113.10",)
                )),
                mock.patch("flayr_core.llm.api.run_command", side_effect=fake_run),
                mock.patch("flayr_core.llm.api.time.sleep"),
            ):
                raw = call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                )

            self.assertEqual(len(calls), 1)
            self.assertIn('"finish_reason": "length"', raw)
            self.assertFalse(raw_path.exists())

    def test_qwen_completion_budget_escalates_without_switching_field(self) -> None:
        payload = {"model": "qwen3.6-plus", "max_completion_tokens": 32768}
        old_budget, new_budget = increase_output_budget(payload)
        self.assertEqual((old_budget, new_budget), (32768, 65536))
        self.assertEqual(payload, {"model": "qwen3.6-plus", "max_completion_tokens": 65536})

    def test_generic_budget_keeps_legacy_max_tokens_ceiling(self) -> None:
        payload = {"model": "other-model", "max_tokens": 16384}
        old_budget, new_budget = increase_output_budget(payload)
        self.assertEqual((old_budget, new_budget), (16384, 32768))
        self.assertEqual(payload, {"model": "other-model", "max_tokens": 32768})

    def test_sse_parser_consumes_split_and_multiline_events(self) -> None:
        parser = IncrementalSSEParser(max_event_bytes=1024, max_total_bytes=4096)
        parser.feed(b"event: message\rdata: {\"choices\":[{\"delta\":\r\n")
        parser.feed('data: {"content":"你"}}]}\n\n'.encode("utf-8"))
        parser.feed(
            (
                'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}],\n'
                'data: "usage":{"total_tokens":3}}\n\n'
            ).encode("utf-8")
        )
        parser.feed(b"data: [DONE]\n\n")
        parser.finish()

        content, usage, complete, finish_reason, error = parser.result()
        self.assertEqual(content, "你好")
        self.assertEqual(usage, {"total_tokens": 3})
        self.assertTrue(complete)
        self.assertEqual(finish_reason, "stop")
        self.assertIsNone(error)

    def test_sse_parser_rejects_malformed_json(self) -> None:
        parser = IncrementalSSEParser(max_event_bytes=1024, max_total_bytes=4096)
        parser.feed(b"data: {not-json}\n\n")
        parser.finish()
        self.assertIn("invalid SSE JSON event", parser.result()[-1] or "")

    def test_http_status_marker_is_structured_and_strippable(self) -> None:
        stderr = "curl: progress\n__FLAYR_HTTP_STATUS__503\n"
        self.assertEqual(parse_curl_http_status(stderr), 503)
        self.assertNotIn("__FLAYR_HTTP_STATUS__", strip_curl_http_status(stderr))
        self.assertIsNone(parse_curl_http_status("curl: failed"))

    def test_redirect_is_hard_failure_until_revalidated(self) -> None:
        self.assertFalse(is_retryable_error("HTTP 302", http_status=302))


if __name__ == "__main__":
    unittest.main()
