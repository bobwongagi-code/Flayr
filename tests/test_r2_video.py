import hashlib
import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts.flayr_core.llm import r2_video as r2
from scripts.flayr_core.network import ValidatedOutboundURL


class R2VideoTests(unittest.TestCase):
    def config(self):
        return r2.R2VideoConfig("a" * 32, "video-test", "private-test-token")

    def transfer(self, downloaded):
        calls = []
        def request(url, config, **kwargs):
            calls.append((url, config))
            if len(calls) == 1:
                return json.dumps({"success": True, "result": {"status": "active", "id": "b" * 32}})
            if kwargs.get("callback"):
                kwargs["callback"](downloaded)
            return ""
        return calls, request

    def test_url_is_returned_only_after_source_hash_verification(self):
        calls, request = self.transfer(b"video bytes")
        with patch.object(r2, "_request", side_effect=request):
            uri, digest = r2.upload_video_url(b"video bytes", self.config())
        self.assertEqual(digest, hashlib.sha256(b"video bytes").hexdigest())
        self.assertEqual(len(calls), 3)
        self.assertIn(digest, uri)
        self.assertIn("X-Amz-Signature=", uri)
        self.assertIn("aws-sigv4", calls[1][1])
        self.assertNotIn(self.config().token, uri)

    def test_corrupt_object_is_not_sent_to_model(self):
        _, request = self.transfer(b"wrong bytes")
        with patch.object(r2, "_request", side_effect=request), self.assertRaisesRegex(ValueError, "identity mismatch"):
            r2.upload_video_url(b"video bytes", self.config())

    def test_credentials_and_signed_url_are_not_process_arguments(self):
        uri = "https://a.r2.cloudflarestorage.com/path?X-Amz-Signature=private"
        validated = ValidatedOutboundURL(uri, "a.r2.cloudflarestorage.com", 443, ("1.1.1.1",))
        with patch.object(r2, "validate_outbound_url", return_value=validated), patch.object(
            r2, "run_command", return_value=subprocess.CompletedProcess([], 0, "ok", "__FLAYR_R2_STATUS__200")
        ) as command:
            r2._request(uri, 'user = "private"\n', budget=None)
        args, kwargs = command.call_args
        self.assertNotIn(uri, args[0])
        self.assertNotIn("private", " ".join(args[0]))
        self.assertIn(uri, kwargs["stdin_text"])
        self.assertIn("--resolve", args[0])
        self.assertGreater(kwargs["max_output_bytes"], int(args[0][args[0].index("--max-filesize") + 1]))

    def test_redirect_or_missing_status_is_not_success(self):
        url = "https://a.r2.cloudflarestorage.com/path"
        validated = ValidatedOutboundURL(url, "a.r2.cloudflarestorage.com", 443, ("1.1.1.1",))
        for stderr in ("__FLAYR_R2_STATUS__302", ""):
            with patch.object(r2, "validate_outbound_url", return_value=validated), patch.object(
                r2, "run_command", return_value=subprocess.CompletedProcess([], 0, "", stderr)
            ), self.assertRaises(ValueError):
                r2._request(url, "", budget=None)

    def test_bad_configuration_and_source_size_fail_without_network(self):
        with self.assertRaises(ValueError):
            r2.R2VideoConfig("evil", "video-test", "token")
        with self.assertRaises(ValueError):
            r2.R2VideoConfig("a" * 32, "video-test", "token\nheader=x")
        with patch.object(r2, "_request") as request, self.assertRaises(ValueError):
            r2.upload_video_url(b"", self.config())
        request.assert_not_called()

    def test_signed_query_is_deterministic_and_scoped(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        url = "https://example.r2.cloudflarestorage.com/bucket/video.mp4"
        uri = r2._presigned_get(url, "access", "secret", now)
        self.assertEqual(uri, r2._presigned_get(url, "access", "secret", now))
        self.assertIn("X-Amz-Expires=3600", uri)
        self.assertIn("20261006%2Fauto%2Fs3%2Faws4_request", uri)


if __name__ == "__main__":
    unittest.main()
