import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.flayr_core.llm import api, gemini_transport as transport
from scripts.flayr_core.network import ValidatedOutboundURL


class GeminiTransportTests(unittest.TestCase):
    def payload(self):
        return {"model": "gemini-3.7-flash", "messages": [{"role": "user", "content": "source"}]}

    def response(self, content='{"value":1}', finish="STOP"):
        candidate = {"content": {"parts": [{"text": content}]}}
        if finish:
            candidate["finishReason"] = finish
        return json.dumps({"modelVersion": "gemini-3.7-flash", "candidates": [candidate], "usageMetadata": {}})

    def call(self, folder, **kwargs):
        options = dict(max_time_seconds=600, retries=2, budget=None, call_kind="llm",
                       request_id="test", response_meta={}, cleanup_raw=False)
        options.update(kwargs)
        return transport.call_native_completion("test-private-key", self.payload(), Path(folder) / "response.json", **options)

    def mock_network(self):
        url = "https://rootifyaiapi.rootifyglobal.com/v1beta/models/gemini-3.7-flash:generateContent"
        return patch.object(transport, "validate_outbound_url", return_value=ValidatedOutboundURL(
            url, "rootifyaiapi.rootifyglobal.com", 443, ("1.1.1.1",)))

    def test_standard_completion_and_private_auth_stdin(self):
        with tempfile.TemporaryDirectory() as folder, self.mock_network(), patch.object(
            transport, "run_command", return_value=subprocess.CompletedProcess([], 0, self.response(), "__FLAYR_HTTP_STATUS__200")
        ) as command:
            value = json.loads(self.call(folder))
            self.assertEqual(value["choices"][0]["message"]["content"], '{"value":1}')
            self.assertTrue((Path(folder) / "response.json").exists())
            self.assertNotIn("test-private-key", str(command.call_args.args))
            self.assertIn("test-private-key", command.call_args.kwargs["stdin_text"])

    def test_plain_ocr_and_translation_do_not_require_json(self):
        for kind in ("ocr", "translation"):
            with tempfile.TemporaryDirectory() as folder, self.mock_network(), patch.object(
                transport, "run_command", return_value=subprocess.CompletedProcess([], 0, self.response("literal transcript"), "__FLAYR_HTTP_STATUS__200")
            ):
                value = json.loads(self.call(folder, call_kind=kind))
                self.assertEqual(value["choices"][0]["message"]["content"], "literal transcript")

    def test_consecutive_504_have_three_identical_attempts(self):
        bodies = []
        def send(command, **kwargs):
            path = command[command.index("--data-binary") + 1][1:]
            bodies.append(Path(path).read_bytes())
            return subprocess.CompletedProcess([], 22, "", "__FLAYR_HTTP_STATUS__504")
        with tempfile.TemporaryDirectory() as folder, self.mock_network(), patch.object(
            transport, "run_command", side_effect=send
        ), patch.object(transport.time, "sleep"), self.assertRaises(SystemExit):
            self.call(folder)
            self.fail("Must fail, not fabricate a completion")
        self.assertEqual(len(bodies), 3)
        self.assertEqual(len(set(bodies)), 1)

    def test_refusal_and_truncation_are_not_transport_retried(self):
        for text, finish in (("The prompt could not be submitted.", None), ('{"partial":', "MAX_TOKENS")):
            with tempfile.TemporaryDirectory() as folder, self.mock_network(), patch.object(
                transport, "run_command", return_value=subprocess.CompletedProcess([], 0, self.response(text, finish), "__FLAYR_HTTP_STATUS__200")
            ) as command, self.assertRaises(SystemExit):
                self.call(folder)
            self.assertEqual(command.call_count, 1)

    def test_normal_api_explicit_gemini_dispatch_and_payload_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            request, response = Path(folder) / "request.json", Path(folder) / "response.json"
            request.write_text(json.dumps(self.payload()))
            with patch.object(transport, "call_native_completion", return_value="completion") as native:
                self.assertEqual(api.call_llm_api(api.DEFAULT_COMPANY_LLM_API_URL, "private", request, response), "completion")
            self.assertFalse(request.exists())
            self.assertEqual(native.call_count, 1)
            self.assertTrue(api.can_analyze_native_video(api.DEFAULT_COMPANY_LLM_API_URL, "gemini-3.7-flash"))
            self.assertFalse(api.can_analyze_native_video(api.DEFAULT_COMPANY_LLM_API_URL, "gpt-6-sol"))

    def test_video_wire_uses_verified_url_not_inline_bytes(self):
        payload = self.payload()
        payload["messages"][0]["content"] = [
            {"type": "text", "text": "source"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(b"video").decode()}},
        ]
        bodies = []
        def send(command, **kwargs):
            path = command[command.index("--data-binary") + 1][1:]
            bodies.append(json.loads(Path(path).read_text()))
            return subprocess.CompletedProcess([], 0, self.response(), "__FLAYR_HTTP_STATUS__200")
        with tempfile.TemporaryDirectory() as folder, self.mock_network(), patch.object(
            self, "payload", return_value=payload
        ), patch.object(transport.R2VideoConfig, "from_environment"), patch.object(
            transport, "upload_video_url", return_value=("https://example.r2.cloudflarestorage.com/video?signature=private", hashlib.sha256(b"video").hexdigest())
        ) as upload, patch.object(transport, "run_command", side_effect=send):
            self.call(folder)
        upload.assert_called_once()
        self.assertEqual(bodies[0]["contents"][0]["parts"][1]["fileData"]["mimeType"], "video/mp4")
        self.assertNotIn("inlineData", str(bodies[0]))


if __name__ == "__main__":
    unittest.main()
