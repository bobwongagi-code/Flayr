from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.speech_mode import classify_speech_mode, speech_mode_prompt  # noqa: E402


class SpeechModeObservabilityTests(unittest.TestCase):
    def test_failed_and_empty_asr_remain_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            audio = role_dir / "audio.wav"
            audio.write_bytes(b"audio")

            for status, transcript in (("failed", ""), ("completed", "（空）"), (None, "")):
                with self.subTest(status=status, transcript=transcript):
                    (role_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
                    info = {"audio_path": str(audio)}
                    if status is not None:
                        info["transcription_status"] = status

                    result = classify_speech_mode(role_dir, info)

                    self.assertEqual(result["mode"], "visual_driven")
                    self.assertEqual(result["speech_evidence_status"], "unknown")
                    self.assertEqual(result["transcription_status"], status)
                    self.assertEqual(
                        result["primary_evidence"],
                        ["selection_report", "timeline_views", "visual_frames", "shot_track"],
                    )
                    self.assertEqual(
                        result["reason"],
                        "未检测到有效口播和 OCR 字幕轨；用画面变化、镜头轨和时间线视图组织证据。",
                    )

    def test_music_label_is_observed_separately_from_empty_failed_asr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            audio = role_dir / "audio.wav"
            audio.write_bytes(b"audio")
            transcript_path = role_dir / "transcript.txt"
            transcript_path.write_text("[MUSIC]", encoding="utf-8")

            music = classify_speech_mode(
                role_dir,
                {"audio_path": str(audio), "transcription_status": "completed"},
            )
            self.assertEqual(music["mode"], "music_driven")
            self.assertEqual(music["speech_evidence_status"], "non_speech_label")
            self.assertEqual(
                music["primary_evidence"],
                ["timeline_views", "audio_waveform", "selection_report", "visual_frames"],
            )
            self.assertEqual(
                music["reason"],
                "转写结果为音乐/环境声标签；用画面和音频节奏组织证据。",
            )

            transcript_path.write_text("", encoding="utf-8")
            failed = classify_speech_mode(
                role_dir,
                {"audio_path": str(audio), "transcription_status": "failed"},
            )
            self.assertEqual(failed["mode"], "visual_driven")
            self.assertEqual(failed["speech_evidence_status"], "unknown")

    def test_matching_sha_attestation_marks_speech_verified_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            source = role_dir / "source.mp4"
            source.write_bytes(b"source video")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            info = {
                "transcription_status": "verified_no_product_speech",
                "preprocess_fingerprint": {"source_video": {"sha256": source_sha256}},
                "no_product_speech_attestation": {
                    "status": "verified_no_product_speech",
                    "scope": "product_speech",
                    "source_sha256": source_sha256,
                    "attestation_source": "user_confirmation",
                    "attestation_statement": "No product speech in this source.",
                    "attestation_file": {"path": "attestation.json", "sha256": "a" * 64},
                },
            }

            result = classify_speech_mode(role_dir, info)

            self.assertEqual(result["mode"], "music_driven")
            self.assertEqual(result["speech_evidence_status"], "verified_absent")
            self.assertEqual(result["transcription_status"], "verified_no_product_speech")
            self.assertEqual(
                result["primary_evidence"],
                ["timeline_views", "audio_waveform", "selection_report", "visual_frames"],
            )
            self.assertEqual(
                result["reason"],
                "人工已确认完整源视频不存在产品口播；歌词不作为产品转写。",
            )

    def test_spoken_and_subtitle_routing_and_prompt_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spoken_dir = root / "spoken"
            spoken_dir.mkdir()
            (spoken_dir / "transcript.txt").write_text("Words spoken in this video.", encoding="utf-8")
            spoken = classify_speech_mode(spoken_dir, {"transcription_status": " completed "})

            subtitle_dir = root / "subtitle"
            subtitle_dir.mkdir()
            (subtitle_dir / "transcript.txt").write_text("", encoding="utf-8")
            (subtitle_dir / "subtitle_track.json").write_text(
                json.dumps({"segments": [{"text": "visible copy"}]}),
                encoding="utf-8",
            )
            subtitle = classify_speech_mode(subtitle_dir, {"transcription_status": "failed"})

        expected = (
            (
                spoken,
                "spoken",
                ["transcript_windowed", "transcript_srt", "timeline_views", "visual_frames"],
                "有效口播存在；SRT segments=0。",
                "present",
                "speech_mode=spoken；reason=有效口播存在；SRT segments=0。\n"
                "证据优先级必须随 speech_mode 切换：有有效口播时，以 transcript_windowed（词级时间线）"
                "为窗口归因骨架，以原始 transcript.srt 做逐字审计；画面、OCR、波形用于校验声画是否对齐。",
            ),
            (
                subtitle,
                "subtitle_driven",
                ["subtitle_track", "timeline_views", "selection_report", "visual_frames"],
                "未检测到有效口播，但 OCR 字幕轨有 1 段。",
                "unknown",
                "speech_mode=subtitle_driven；reason=未检测到有效口播，但 OCR 字幕轨有 1 段。\n"
                "证据优先级必须随 speech_mode 切换：无有效口播但有字幕时，以 subtitle_track/OCR 作为文案轨，"
                "画面变化和 timeline view 校验字幕承诺是否被画面支撑；voiceover 必须留空，不得把屏幕字幕写成口播。",
            ),
        )
        for result, mode, primary_evidence, reason, evidence_status, expected_prompt in expected:
            with self.subTest(mode=mode):
                self.assertEqual(result["mode"], mode)
                self.assertEqual(result["primary_evidence"], primary_evidence)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["speech_evidence_status"], evidence_status)
                self.assertEqual(speech_mode_prompt(result), expected_prompt)
                self.assertEqual(
                    speech_mode_prompt(result),
                    speech_mode_prompt({"mode": result["mode"], "reason": result["reason"]}),
                )
        self.assertEqual(spoken["transcription_status"], " completed ")


if __name__ == "__main__":
    unittest.main()
