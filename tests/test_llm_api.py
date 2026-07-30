"""Focused contracts for provider capability lookup and incremental SSE parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.api import (  # noqa: E402
    IncrementalSSEParser,
    can_analyze_native_audio,
    can_send_standalone_audio,
    call_llm_api,
    increase_output_budget,
    is_retryable_error,
    parse_curl_http_status,
    provider_capabilities,
    strip_curl_http_status,
)
from flayr_core.llm.media import build_evidence_sensory_inputs  # noqa: E402
from flayr_core.llm.payload import build_video_fact_payload  # noqa: E402


class LlmApiContractTests(unittest.TestCase):
    def test_known_provider_capabilities_are_explicit(self) -> None:
        qwen_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        qwen = provider_capabilities(qwen_url, "qwen3-omni-flash")
        self.assertEqual(qwen.profile, "dashscope_qwen_compatible")
        self.assertEqual(qwen.confidence, "verified_matrix")
        self.assertTrue(can_send_standalone_audio(qwen_url, "qwen3-omni-flash"))
        self.assertTrue(can_analyze_native_audio(qwen_url, "qwen3-omni-flash"))

    def test_beijing_maas_qwen_capabilities_are_explicit(self) -> None:
        qwen_url = "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
        qwen = provider_capabilities(qwen_url, "qwen3.6-plus")
        self.assertEqual(qwen.profile, "beijing_maas_qwen_transcript_visual")
        self.assertEqual(qwen.confidence, "runtime_verified")
        self.assertFalse(can_send_standalone_audio(qwen_url, "qwen3.6-plus"))
        self.assertFalse(can_analyze_native_audio(qwen_url, "qwen3.6-plus"))

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
            video_encoder.assert_not_called()

    def test_unknown_provider_is_conservative(self) -> None:
        capabilities = provider_capabilities("https://example.test/v1/chat/completions", "vision-test")
        self.assertEqual(capabilities.profile, "unknown_provider")
        self.assertEqual(capabilities.confidence, "unverified")
        self.assertFalse(can_send_standalone_audio("https://example.test/v1/chat/completions", "vision-test"))
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
