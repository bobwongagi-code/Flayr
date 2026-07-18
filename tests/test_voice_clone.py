from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts.flayr_core.voice_clone import _synthesize_lines


class VoiceCloneIsolationTests(unittest.TestCase):
    def _run_synthesis(self, *, initial_api_key: str | None, initial_ssl: str | None) -> types.ModuleType:
        dashscope = types.ModuleType("dashscope")
        if initial_api_key is not None:
            dashscope.api_key = initial_api_key
        audio_package = types.ModuleType("dashscope.audio")
        tts_module = types.ModuleType("dashscope.audio.tts_v2")

        class FakeSynthesizer:
            def __init__(self, **_: object) -> None:
                pass

            def call(self, _: str) -> bytes:
                return b"audio"

        class FakeAudioFormat:
            WAV_24000HZ_MONO_16BIT = "wav"

        tts_module.SpeechSynthesizer = FakeSynthesizer
        tts_module.AudioFormat = FakeAudioFormat
        certifi = types.ModuleType("certifi")
        certifi.where = lambda: "/temporary/ca.pem"
        modules = {
            "dashscope": dashscope,
            "dashscope.audio": audio_package,
            "dashscope.audio.tts_v2": tts_module,
            "certifi": certifi,
        }

        environment = {} if initial_ssl is None else {"SSL_CERT_FILE": initial_ssl}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, modules), mock.patch.dict(
            os.environ, environment, clear=False
        ):
            if initial_ssl is None:
                os.environ.pop("SSL_CERT_FILE", None)
            outputs = _synthesize_lines(
                Path(tmp),
                "voice-id",
                [{"id": "p1", "text": "hello"}],
                "temporary-key",
            )
            self.assertEqual(outputs[0]["id"], "p1")
            if initial_ssl is None:
                self.assertNotIn("SSL_CERT_FILE", os.environ)
            else:
                self.assertEqual(os.environ["SSL_CERT_FILE"], initial_ssl)
        return dashscope

    def test_synthesis_restores_existing_process_wide_sdk_state(self) -> None:
        dashscope = self._run_synthesis(initial_api_key="original-key", initial_ssl="/original/ca.pem")
        self.assertEqual(dashscope.api_key, "original-key")

    def test_synthesis_removes_temporary_process_wide_sdk_state(self) -> None:
        dashscope = self._run_synthesis(initial_api_key=None, initial_ssl=None)
        self.assertFalse(hasattr(dashscope, "api_key"))


if __name__ == "__main__":
    unittest.main()
