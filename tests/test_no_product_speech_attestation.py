from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr  # noqa: E402
from flayr_core.run_manifest import BASE_REQUIRED_ARTIFACTS, build_success_manifest  # noqa: E402
from flayr_core.speech_attestation import has_verified_no_product_speech  # noqa: E402
from flayr_core.prompt import speech_status, write_analysis_input  # noqa: E402
from flayr_core.llm.parse import normalize_video_fact_result  # noqa: E402
from flayr_core.stage_evidence_contracts import (  # noqa: E402
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE1_ACQUISITION_VERSION,
    build_stage1_acquisition_manifest,
    stage1_acquisition_issues,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attestation(source: Path) -> dict[str, object]:
    return {
        "status": "verified_no_product_speech",
        "scope": "product_speech",
        "source_sha256": _sha256(source),
        "attestation_source": "user_confirmation",
        "attestation_statement": "Human confirmed no product speech in this exact source.",
    }


class NoProductSpeechAttestationTests(unittest.TestCase):
    def test_preprocessing_skips_asr_and_translation_but_continues_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"music-only input")
            attestation_file = root / "attestations.json"
            record = _attestation(source)
            attestation_file.write_text(
                json.dumps({"verified_no_product_speech_attestations": [record]}),
                encoding="utf-8",
            )
            output = flayr._load_verified_no_product_speech_attestations(
                attestation_file,
                {"creator": source},
            )
            args = SimpleNamespace(
                asr_language="auto",
                asr_api_url="https://example.test/asr",
                asr_model="test-asr",
                translate_with_llm=True,
                translation_model="test-translation",
                llm_model="",
                judgment_model="test-judge",
                vision_model="test-vision",
                llm_api_url="https://example.test/v1/chat/completions",
                product_name="Test product",
                product_notes="",
                ocr_mode="on",
                with_ocr=True,
                no_ocr=False,
                llm_dry_run=False,
                legacy_import=False,
                _verified_no_product_speech_attestations=output,
            )
            run_dir = root / "run"
            deps = {"ffmpeg": "ffmpeg", "ffprobe": None}

            def extract_audio(_source: Path, audio_path: Path, result: dict[str, object]) -> None:
                audio_path.write_bytes(b"audio")
                result["audio_path"] = str(audio_path)

            def build_ocr(role_dir: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
                (role_dir / "subtitle_track.json").write_text(
                    json.dumps({"segments": [{"text": "visible text"}]}),
                    encoding="utf-8",
                )
                return {"status": "ready", "segments": [{"text": "visible text"}]}

            with (
                mock.patch.object(flayr, "_binary_version", return_value="ffmpeg:test"),
                mock.patch.object(flayr, "_preprocess_implementation_sha256", return_value="a" * 64),
                mock.patch.object(flayr, "extract_frames"),
                mock.patch.object(flayr, "extract_audio", side_effect=extract_audio),
                mock.patch.object(flayr, "analyze_audio_quality", return_value={}),
                mock.patch.object(flayr, "run_online_asr") as asr_call,
                mock.patch.object(flayr, "sync_chinese_translation") as translation_sync,
                mock.patch.object(flayr, "translate_transcript_with_llm") as translation_call,
                mock.patch.object(flayr, "build_subtitle_track", side_effect=build_ocr) as ocr_call,
                mock.patch.object(flayr, "compute_shake_metric", return_value={}),
                mock.patch.object(flayr, "build_shot_track", return_value={"status": "skipped", "shots": []}),
                mock.patch.object(flayr, "extract_anchor_frames"),
                mock.patch.object(flayr, "resolve_ocr_policy", return_value=(True, "ocr-key", "")),
                mock.patch.object(flayr, "build_video_evidence_artifacts", return_value={}),
                mock.patch.object(flayr, "ensure_video_evidence_artifacts"),
            ):
                result = flayr.process_video("creator", source, run_dir, deps, args)
                args.reuse_preprocessing = True
                reused = flayr.process_video("creator", source, run_dir, deps, args)

            asr_call.assert_not_called()
            translation_sync.assert_not_called()
            translation_call.assert_not_called()
            ocr_call.assert_called_once()
            self.assertEqual(result["transcription_status"], "verified_no_product_speech")
            self.assertTrue(has_verified_no_product_speech(result))
            self.assertIsNone(result["transcript_path"])
            self.assertIsNone(result["transcript_segments_path"])
            self.assertEqual(result["translation_status"], "not_requested_no_product_speech")
            self.assertEqual(result["speech_mode"]["mode"], "subtitle_driven")
            self.assertEqual(reused["transcription_status"], "verified_no_product_speech")
            self.assertFalse((run_dir / "creator" / "transcript.txt").exists())
            self.assertFalse((run_dir / "creator" / "transcript.srt").exists())
            self.assertEqual(flayr._transcription_issues({"creator": result}), [])

    def test_attestation_requires_exact_source_sha_and_does_not_relax_failed_asr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"expected")
            manifest = root / "attestations.json"
            record = _attestation(source)
            manifest.write_text(
                json.dumps({"verified_no_product_speech_attestations": [record]}),
                encoding="utf-8",
            )
            source.write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "does not match any exact input"):
                flayr._load_verified_no_product_speech_attestations(manifest, {"creator": source})

            self.assertEqual(
                flayr._transcription_issues(
                    {"creator": {"transcription_status": "failed", "errors": ["HTTP 400"]}}
                ),
                ["creator: online Fun-ASR transcription_status=failed"],
            )

    def test_completion_accepts_only_valid_hash_bound_no_speech_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "creator.mp4"
            source.write_bytes(b"creator")
            attestation_file = root / "attestations.json"
            record = _attestation(source)
            attestation_file.write_text(
                json.dumps({"verified_no_product_speech_attestations": [record]}),
                encoding="utf-8",
            )
            verified = flayr._load_verified_no_product_speech_attestations(
                attestation_file,
                {"creator": source},
            )["creator"]
            info = {
                "transcription_status": "verified_no_product_speech",
                "no_product_speech_attestation": verified,
                "transcript_path": None,
                "transcript_segments_path": None,
                "transcript_words_path": None,
                "preprocess_fingerprint": {
                    "source_video": {"sha256": _sha256(source)},
                },
            }
            run_dir = root / "run"
            run_dir.mkdir()
            for artifact in BASE_REQUIRED_ARTIFACTS:
                path = run_dir / artifact
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "postprocess_provenance": {
                                "field_sources": {
                                    "coverage": "complete",
                                    "unresolved_paths": [],
                                    "truncated": False,
                                }
                            }
                        }
                    ) if artifact == "final_derived_result.json" else "{}\n",
                    encoding="utf-8",
                )
            analysis = {
                "analysis_run_state": "completed",
                "mode": "analyze",
                "videos": {"creator": info},
            }
            result = build_success_manifest(run_dir, {"creator_video": source}, analysis)
            self.assertEqual(result["status"], "completed")

            failed = {**analysis, "videos": {"creator": {**info, "transcription_status": "failed"}}}
            with self.assertRaisesRegex(ValueError, "verified no-product-speech"):
                build_success_manifest(run_dir, {"creator_video": source}, failed)
            wrong_source = {
                **analysis,
                "videos": {
                    "creator": {
                        **info,
                        "no_product_speech_attestation": {**verified, "source_sha256": "0" * 64},
                    }
                },
            }
            with self.assertRaisesRegex(ValueError, "verified no-product-speech"):
                build_success_manifest(run_dir, {"creator_video": source}, wrong_source)

    def test_stage_evidence_uses_attested_full_clip_coverage_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = root / "frame.jpg"
            audio = root / "audio.wav"
            source = root / "source.mp4"
            attestation_file = root / "attestations.json"
            for path in (frame, audio):
                path.write_bytes(b"artifact")
            source.write_bytes(b"source")
            record = _attestation(source)
            attestation_file.write_text(
                json.dumps({"verified_no_product_speech_attestations": [record]}),
                encoding="utf-8",
            )
            attestation = flayr._load_verified_no_product_speech_attestations(
                attestation_file,
                {"creator": source},
            )["creator"]
            info = {
                "work_dir": str(root),
                "duration_seconds": 5,
                "audio_path": str(audio),
                "transcription_status": "verified_no_product_speech",
                "no_product_speech_attestation": attestation,
                "preprocess_fingerprint": {"source_video": {"sha256": _sha256(source)}},
                "speech_mode": {"mode": "music_driven"},
                "video_evidence": {
                    "analysis_frames": [{"timestamp_seconds": 1, "path": str(frame)}],
                },
            }
            manifest = build_stage1_acquisition_manifest({"videos": {"creator": info}}, "creator")
        self.assertEqual(STAGE_EVIDENCE_CONTRACT_VERSION, 10)
            self.assertEqual(STAGE1_ACQUISITION_VERSION, 6)
            self.assertEqual(manifest["channels"]["voiceover"]["status"], "ready")
            self.assertEqual(manifest["channels"]["voiceover"]["coverage"], "full")
            self.assertEqual(
                manifest["channels"]["voiceover"]["boundary_precision"],
                "human_attested_full_video",
            )
            self.assertEqual(manifest["voiceover_attestation_sha256"], _sha256(source))

            positive_voiceover = {
                "stage1_acquisition": manifest,
                "evidence_units": [{"id": "C1", "voiceover": "fabricated lyric", "time_range": "1s - 2s"}],
                "stage_evidence_checks": [{"stage": "S1", "status": "present", "evidence_ids": ["C1"]}],
            }
            self.assertIn(
                "S1:acquisition_channel_boundary_imprecise:voiceover",
                stage1_acquisition_issues(positive_voiceover, "S1"),
            )

            full_visual_manifest = build_stage1_acquisition_manifest(
                {"videos": {"creator": info}},
                "creator",
                native_video=True,
                native_video_windows=[{"start_seconds": 0, "end_seconds": 5}],
            )
            negative_stage = {
                "stage1_acquisition": full_visual_manifest,
                "stage_evidence_checks": [{"stage": "S1", "status": "absent", "evidence_ids": []}],
            }
            self.assertEqual(stage1_acquisition_issues(negative_stage, "S1"), [])

            info.pop("no_product_speech_attestation")
            unknown = build_stage1_acquisition_manifest({"videos": {"creator": info}}, "creator")
            self.assertEqual(unknown["channels"]["voiceover"]["status"], "unknown")
            self.assertEqual(unknown["speech_mode"], "unknown")

    def test_prompt_distinguishes_attestation_from_unverified_missing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "creator"
            role_dir.mkdir()
            source = role_dir / "video.mp4"
            source.write_bytes(b"music-only input")
            attestation_file = root / "attestations.json"
            attestation_file.write_text(
                json.dumps({"verified_no_product_speech_attestations": [_attestation(source)]}),
                encoding="utf-8",
            )
            attestation = flayr._load_verified_no_product_speech_attestations(
                attestation_file,
                {"creator": source},
            )["creator"]
            source_sha256 = _sha256(source)
            attestation_file_sha256 = _sha256(attestation_file)
            info = {
                "path": str(source),
                "work_dir": str(role_dir),
                "duration_seconds": 5.0,
                "transcription_status": "verified_no_product_speech",
                "no_product_speech_attestation": attestation,
                "transcript_path": None,
                "transcript_segments_path": None,
                "transcript_words_path": None,
                "preprocess_fingerprint": {"source_video": {"sha256": source_sha256}},
                "frames_dir": str(role_dir / "frames"),
                "focus_frames_dir": str(role_dir / "focus_frames"),
                "video_evidence": {},
            }
            analysis = {
                "analysis_scope": {"label": "视频证据分析", "missing_context": [], "boundary": "仅按视频事实判断"},
                "product": {
                    "name": "测试品",
                    "category": "",
                    "price": "",
                    "target_market": "auto",
                    "core_selling_points": "",
                    "target_user": "",
                    "purchase_motivation": "",
                    "creator_profile": "",
                    "notes": "",
                },
                "videos": {"creator": info},
            }
            prompt = write_analysis_input(root, analysis).read_text(encoding="utf-8")

            self.assertIn("人工已确认绑定源 SHA-256 的完整视频没有产品口播", speech_status(role_dir, info))
            self.assertIn(source_sha256, prompt)
            self.assertIn(attestation_file_sha256, prompt)
            self.assertIn("ASR/翻译未请求；无转写文本、分段或口播时间戳。", prompt)
            for name in ("transcript.txt", "transcript.srt", "transcript.words.json"):
                self.assertNotIn(str(role_dir / name), prompt)

            failed_status = speech_status(role_dir, {"transcription_status": "failed"})
            self.assertIn("口播状态未知", failed_status)
            self.assertNotIn("未检测到有效口播", failed_status)

    def test_stage1_parser_strips_short_model_voiceover_under_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"music-only input")
            attestation_file = root / "attestations.json"
            attestation_file.write_text(
                json.dumps({"verified_no_product_speech_attestations": [_attestation(source)]}),
                encoding="utf-8",
            )
            attestation = flayr._load_verified_no_product_speech_attestations(
                attestation_file,
                {"creator": source},
            )["creator"]
            analysis = {
                "videos": {
                    "creator": {
                        "work_dir": str(root),
                        "duration_seconds": 5.0,
                        "transcription_status": "verified_no_product_speech",
                        "no_product_speech_attestation": attestation,
                        "preprocess_fingerprint": {"source_video": {"sha256": _sha256(source)}},
                    }
                }
            }
            normalized = normalize_video_fact_result(
                "creator",
                {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": "1s - 2s",
                            "information": "",
                            "visual_fact": "Creator holds the product.",
                            "voiceover": "Buy now",
                            "voiceover_zh": "现在购买",
                            "evidence_strength": "direct",
                        }
                    ]
                },
                analysis,
            )
            unit = normalized["evidence_units"][0]
            self.assertEqual(unit["voiceover"], "")
            self.assertEqual(unit["voiceover_zh"], "")
            self.assertEqual(unit["information"], "Creator holds the product.")

if __name__ == "__main__":
    unittest.main()
