"""Audio hard-QC and observation-only policy tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.audio_quality import analyze_audio_quality
from flayr_core.llm.api import can_analyze_native_audio
from flayr_core.multimodal import multimodal_execution, sanitize_audio_observations
from flayr_core.report import render_audio_quality_rows


class AudioLayerTests(unittest.TestCase):
    def test_dashscope_qwen_audio_uses_native_analysis_with_local_transcript_fallback(self) -> None:
        self.assertTrue(
            can_analyze_native_audio(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "qwen3-omni-flash",
            )
        )

    def test_sound_only_observation_never_changes_execution(self) -> None:
        stage = {
            "creator_multimodal": {
                "channel_impacts": {
                    "visual": "unknown",
                    "speech": "unknown",
                    "text": "unknown",
                    "sound_rhythm": "strong_positive",
                },
                "integrated_effect": "strong",
            }
        }
        self.assertEqual(multimodal_execution("S1", stage, "creator", 0.5), 0.5)
        self.assertEqual(multimodal_execution("S4", stage, "creator", 1.0), 1.0)

    def test_unavailable_audio_judgments_are_removed(self) -> None:
        result = {
            "holistic_assessment": {"pace_and_emotion": "BGM 很有感染力。"},
            "video_understanding": {
                "creator": {"evidence_units": [{"audio_fact": "音乐热烈。"}]},
            },
            "stage_analysis": [
                {
                    "voice_performance": {"pace": "快", "energy": "高", "key_pause": True},
                    "creator_multimodal": {
                        "channel_impacts": {"sound_rhythm": "strong_positive"},
                        "channel_evidence_ids": {"sound_rhythm": ["C1"]},
                        "dominant_channel": "sound_rhythm",
                    },
                }
            ],
        }
        sanitize_audio_observations(result, False)
        stage = result["stage_analysis"][0]
        self.assertEqual(stage["voice_performance"]["pace"], "未评估")
        self.assertEqual(stage["creator_multimodal"]["channel_impacts"]["sound_rhythm"], "unknown")
        self.assertEqual(stage["creator_multimodal"]["channel_evidence_ids"]["sound_rhythm"], [])
        self.assertEqual(stage["creator_multimodal"]["dominant_channel"], "unknown")
        self.assertIn("未直接感知音轨", result["holistic_assessment"]["pace_and_emotion"])

    def test_audio_quality_reports_conservative_hard_issues(self) -> None:
        diagnostic = """
[silencedetect @ 0x1] silence_duration: 7.0
{
  "input_i" : "-38.20",
  "input_tp" : "0.80",
  "input_lra" : "2.10"
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"wav")
            with mock.patch(
                "flayr_core.audio_quality.run_command",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=diagnostic),
            ):
                quality = analyze_audio_quality(audio, 10.0)
        self.assertEqual(quality["status"], "warning")
        self.assertEqual(quality["metrics"]["silence_ratio"], 0.7)
        self.assertEqual(
            {item["code"] for item in quality["hard_issues"]},
            {"audio_too_quiet", "excessive_silence", "peak_overload_risk"},
        )
        rendered = " ".join(render_audio_quality_rows(quality))
        self.assertIn("音频质量需关注", rendered)

    def test_missing_audio_is_blocking_but_does_not_crash(self) -> None:
        quality = analyze_audio_quality(None, 12.0)
        self.assertEqual(quality["status"], "unavailable")
        self.assertEqual(quality["hard_issues"][0]["code"], "audio_missing")


if __name__ == "__main__":
    unittest.main()
