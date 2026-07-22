"""Focused contracts for provider capability lookup and incremental SSE parsing."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.api import (  # noqa: E402
    IncrementalSSEParser,
    can_analyze_native_audio,
    can_send_standalone_audio,
    is_retryable_error,
    parse_curl_http_status,
    provider_capabilities,
    strip_curl_http_status,
)


class LlmApiContractTests(unittest.TestCase):
    def test_known_provider_capabilities_are_explicit(self) -> None:
        qwen_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        qwen = provider_capabilities(qwen_url, "qwen3-omni-flash")
        self.assertEqual(qwen.profile, "dashscope_qwen_compatible")
        self.assertEqual(qwen.confidence, "verified_matrix")
        self.assertTrue(can_send_standalone_audio(qwen_url, "qwen3-omni-flash"))
        self.assertTrue(can_analyze_native_audio(qwen_url, "qwen3-omni-flash"))

    def test_unknown_provider_is_conservative(self) -> None:
        capabilities = provider_capabilities("https://example.test/v1/chat/completions", "vision-test")
        self.assertEqual(capabilities.profile, "unknown_openai_compatible")
        self.assertEqual(capabilities.confidence, "unverified")
        self.assertFalse(can_send_standalone_audio("https://example.test/v1/chat/completions", "vision-test"))
        self.assertFalse(can_analyze_native_audio("https://example.test/v1/chat/completions", "vision-test"))

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
