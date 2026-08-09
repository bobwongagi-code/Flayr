from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.artifacts import (  # noqa: E402
    build_stage_frame_manifest,
    get_frame_entries,
    select_frames_for_time_range,
)
from flayr_core.frame_selection import build_analysis_frame_manifest  # noqa: E402
from flayr_core.llm.media import (  # noqa: E402
    get_llm_frame_candidates,
    get_llm_visual_candidates,
    get_timeline_view_entries,
    select_stage_recovery_visual_inputs,
)
from flayr_core.subtitle_track import _merge_ocr_frame_entries  # noqa: E402
from flayr_core.asr import extract_word_timestamps  # noqa: E402
from flayr_core.video_evidence import (  # noqa: E402
    build_transcript_pack,
    build_timeline_view_for_range,
    build_video_evidence_artifacts,
    select_timeline_transcript,
    write_selection_report_html,
)
from flayr_core.postprocess.repair_stages import infer_s1_boundary_candidate  # noqa: E402
from flayr_core.postprocess.validate import validate_transcript_attribution  # noqa: E402


class VideoEvidenceSelectionTests(unittest.TestCase):
    def _write_frame(self, directory: Path, name: str, color: tuple[int, int, int], local_mark: bool = False) -> Path:
        image = Image.new("RGB", (90, 60), color)
        if local_mark:
            draw = ImageDraw.Draw(image)
            draw.rectangle((60, 20, 86, 46), fill=(255, 255, 255))
        path = directory / name
        image.save(path, format="JPEG")
        return path

    def test_manifest_promotes_scene_subtitle_and_local_change_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            paths = [
                self._write_frame(frames_dir, f"frame_{index:04d}.jpg", (40, 40, 40), index == 2)
                for index in range(6)
            ]
            shot_path = root / "shot_track.json"
            shot_path.write_text(
                json.dumps({"shots": [{"start_sec": 0, "end_sec": 2}, {"start_sec": 2, "end_sec": 6}]}),
                encoding="utf-8",
            )
            subtitle_path = root / "subtitle_track.json"
            subtitle_path.write_text(
                json.dumps({"segments": [{"start_sec": 3.0, "end_sec": 3.5, "text": "价格"}]}),
                encoding="utf-8",
            )
            info = {
                "duration_seconds": 6.0,
                "frames": [
                    {"timestamp_seconds": index, "path": str(path), "filename": path.name}
                    for index, path in enumerate(paths)
                ],
                "shot_track_path": str(shot_path),
                "subtitle_track_path": str(subtitle_path),
            }

            selection = build_analysis_frame_manifest(info)
            reasons_by_time = {
                item.get("timestamp_seconds"): set(item.get("selection_reasons", []))
                for item in selection["frames"]
            }
            self.assertIn("scene_boundary", reasons_by_time.get(0, set()))
            self.assertIn("scene_boundary", reasons_by_time.get(2, set()))
            self.assertIn("subtitle_boundary", reasons_by_time.get(3, set()))
            self.assertTrue(
                any("local_change" in reasons for reasons in reasons_by_time.values()),
                reasons_by_time,
            )

    def test_manifest_rejects_paths_outside_role_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside = root / "frames" / "inside.jpg"
            inside.parent.mkdir()
            inside.write_bytes(b"inside")
            outside = root.parent / f"{root.name}-outside.jpg"
            outside.write_bytes(b"outside")
            try:
                entries = get_frame_entries(
                    {
                        "work_dir": str(root),
                        "frames": [
                            {"path": str(inside), "timestamp_seconds": 0},
                            {"path": str(outside), "timestamp_seconds": 1},
                        ],
                    }
                )
                self.assertEqual([item["path"] for item in entries], [str(inside.resolve())])
            finally:
                outside.unlink(missing_ok=True)

    def test_word_timestamps_promote_speech_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            paths = [self._write_frame(frames_dir, f"frame_{index:04d}.jpg", (index * 20, 40, 40)) for index in range(6)]
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 1.0, "end_seconds": 1.2, "text": "first"},
                            {"start_seconds": 4.0, "end_seconds": 4.2, "text": "last"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            selection = build_analysis_frame_manifest(
                {
                    "work_dir": str(root),
                    "duration_seconds": 5.0,
                    "frames": [
                        {"timestamp_seconds": index, "path": str(path), "filename": path.name}
                        for index, path in enumerate(paths)
                    ],
                    "transcript_words_path": str(words_path),
                }
            )
            reasons_by_time = {
                item.get("timestamp_seconds"): set(item.get("selection_reasons", []))
                for item in selection["frames"]
            }
            self.assertIn("speech_boundary", reasons_by_time.get(1, set()))
            self.assertIn("speech_boundary", reasons_by_time.get(4, set()))

    def test_selection_report_uses_path_relative_to_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "reports" / "selection.html"
            write_selection_report_html(
                report_path,
                {
                    "frame_count": 1,
                    "kept_count": 1,
                    "decisions": [{"path": str(root / "frames" / "frame.jpg"), "filename": "frame.jpg", "kept": True}],
                },
            )
            self.assertIn('../frames/frame.jpg', report_path.read_text(encoding="utf-8"))

    def test_timeline_view_records_canonical_frame_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            focus_dir = root / "focus_frames"
            frames_dir.mkdir()
            focus_dir.mkdir()
            canonical = self._write_frame(frames_dir, "canonical.jpg", (20, 30, 40))
            focus = self._write_frame(focus_dir, "hook_001.jpg", (80, 90, 100))
            view = build_timeline_view_for_range(
                root,
                {
                    "work_dir": str(root),
                    "duration_seconds": 5.0,
                    "analysis_frames": [{"path": str(canonical), "timestamp_seconds": 1.0}],
                    "focus_frames": [{"path": str(focus), "timestamp_seconds": 1.0, "label": "hook"}],
                },
                "hook",
                0.0,
                3.0,
            )
            self.assertIsNotNone(view)
            self.assertEqual(view["frame_paths"], [str(canonical.resolve())])

    def test_timeline_request_provenance_uses_only_verified_canonical_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            timeline_dir = root / "timeline_views"
            frames_dir.mkdir()
            timeline_dir.mkdir()
            canonical = self._write_frame(frames_dir, "canonical.jpg", (20, 30, 40))
            unlisted = self._write_frame(frames_dir, "unlisted.jpg", (80, 90, 100))
            timeline = self._write_frame(timeline_dir, "hook.jpg", (10, 10, 10))
            info = {
                "work_dir": str(root),
                "analysis_frames": [
                    {
                        "path": str(canonical),
                        "timestamp_seconds": 1.0,
                        "selection_reasons": ["first_frame"],
                    }
                ],
                "video_evidence": {
                    "timeline_views": [
                        {
                            "label": "hook",
                            "path": str(timeline),
                            "start_seconds": 0.0,
                            "end_seconds": 2.0,
                            "frame_paths": [str(canonical), str(unlisted)],
                        }
                    ]
                },
            }
            entries = get_timeline_view_entries(info)
            self.assertEqual(entries[0]["source_frame_timestamps"], [1.0])

    def test_visual_candidates_spend_raw_budget_on_uncovered_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            timeline_dir = root / "timeline_views"
            frames_dir.mkdir()
            timeline_dir.mkdir()
            frames = [
                self._write_frame(frames_dir, f"frame_{index}.jpg", (index * 10, 20, 30))
                for index in range(10)
            ]
            timeline = self._write_frame(timeline_dir, "hook.jpg", (10, 10, 10))
            info = {
                "work_dir": str(root),
                "analysis_frames": [
                    {
                        "path": str(path),
                        "timestamp_seconds": float(index),
                        "selection_reasons": ["first_frame" if index == 0 else "density_floor"],
                    }
                    for index, path in enumerate(frames)
                ],
                "video_evidence": {
                    "timeline_views": [
                        {
                            "label": "hook",
                            "path": str(timeline),
                            "start_seconds": 0.0,
                            "end_seconds": 2.0,
                            "frame_paths": [str(frames[0]), str(frames[1]), str(frames[2])],
                        }
                    ]
                },
            }
            candidates = get_llm_visual_candidates(info, 4)
            self.assertEqual(candidates[0]["source_frame_timestamps"], [0.0, 1.0, 2.0])
            raw_timestamps = [item["timestamp_seconds"] for item in candidates[1:]]
            self.assertEqual(raw_timestamps, [3.0, 6.0, 9.0])

    def test_timeline_transcript_prefers_word_window_over_coarse_segment(self) -> None:
        selected = select_timeline_transcript(
            [{"start_seconds": 0.12, "end_seconds": 50.74, "text": "full video transcript"}],
            [
                {"start_seconds": 0.12, "end_seconds": 0.4, "text": "hook"},
                {"start_seconds": 7.0, "end_seconds": 7.3, "text": "later"},
            ],
            0.0,
            6.0,
        )
        self.assertEqual(selected["scope"], "word_window")
        self.assertEqual(selected["word_count"], 1)
        self.assertIn("hook", selected["display_lines"][0])
        self.assertNotIn("full video transcript", selected["display_lines"][0])

    def test_transcript_pack_publishes_raw_and_window_safe_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            srt_path = root / "transcript.srt"
            srt_path.write_text(
                "1\n00:00:00,120 --> 00:00:50,740\nfull video transcript\n",
                encoding="utf-8",
            )
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 0.12, "end_seconds": 0.4, "text": "hook"},
                            {"start_seconds": 0.4, "end_seconds": 0.8, "text": "problem"},
                            {"start_seconds": 7.0, "end_seconds": 7.3, "text": "later"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = build_transcript_pack(
                root,
                {
                    "work_dir": str(root),
                    "transcript_segments_path": str(srt_path),
                    "transcript_words_path": str(words_path),
                },
            )
            self.assertTrue(Path(result["transcript_pack_path"]).is_file())
            windowed = Path(result["transcript_windowed_path"])
            self.assertTrue(windowed.is_file())
            self.assertIn("hook", windowed.read_text(encoding="utf-8"))
            self.assertNotIn("full video transcript", windowed.read_text(encoding="utf-8"))

    def test_transcript_pack_builds_window_safe_view_without_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 0.2, "end_seconds": 0.6, "text": "only words"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = build_transcript_pack(
                root,
                {
                    "work_dir": str(root),
                    "transcript_words_path": str(words_path),
                },
            )
            self.assertNotIn("transcript_pack_path", result)
            self.assertTrue(Path(result["transcript_windowed_path"]).is_file())

    def test_s1_boundary_repair_uses_word_windows_when_srt_is_one_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            srt_path = root / "transcript.srt"
            srt_path.write_text(
                "1\n00:00:00,120 --> 00:00:50,740\nfull video transcript\n",
                encoding="utf-8",
            )
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 0.12, "end_seconds": 0.4, "text": "皮肤问题"},
                            {"start_seconds": 0.4, "end_seconds": 3.5, "text": "继续看"},
                            {"start_seconds": 4.0, "end_seconds": 4.8, "text": "推荐使用这个"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            candidate = infer_s1_boundary_candidate(
                "benchmark",
                {"video_understanding": {"benchmark": {"evidence_units": []}}},
                {
                    "videos": {
                        "benchmark": {
                            "work_dir": str(root),
                            "transcript_segments_path": str(srt_path),
                            "transcript_words_path": str(words_path),
                        }
                    }
                },
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["seconds"], 4.0)
            self.assertEqual(candidate["source"], "transcript_window")

    def test_stage_quote_cannot_use_full_transcript_for_narrow_word_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_text = "hook text followed by later product explanation"
            transcript_path = root / "transcript.txt"
            transcript_path.write_text(transcript_text, encoding="utf-8")
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {"start_seconds": 0.12, "end_seconds": 0.8, "text": "hook text"},
                            {"start_seconds": 7.0, "end_seconds": 8.0, "text": "later product explanation"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            info = {
                "work_dir": str(root),
                "transcript_path": str(transcript_path),
                "transcript_words_path": str(words_path),
            }
            result = {
                "video_understanding": {
                    "benchmark": {
                        "evidence_units": [
                            {
                                "id": "B1",
                                "voiceover": transcript_text,
                                "time_range": "0.12s - 50.74s",
                            }
                        ]
                    },
                    "creator": {
                        "evidence_units": [
                            {
                                "id": "C1",
                                "voiceover": transcript_text,
                                "time_range": "0.12s - 50.74s",
                            }
                        ]
                    },
                },
                "stage_analysis": [
                    {
                        "stage": "S1 Hook",
                        "benchmark_time_range": "0.0s - 6.0s",
                        "creator_time_range": "0.0s - 6.0s",
                        "benchmark_evidence_ids": ["B1"],
                        "creator_evidence_ids": ["C1"],
                        "benchmark_quote": transcript_text,
                        "creator_quote": transcript_text,
                    }
                ],
            }
            with self.assertRaisesRegex(SystemExit, "超出阶段时间窗口"):
                validate_transcript_attribution(
                    result,
                    {"videos": {"benchmark": info, "creator": info}},
                )

    def test_stage_quote_cannot_hide_in_a_window_without_any_timed_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path = root / "transcript.txt"
            transcript_path.write_text("later product explanation", encoding="utf-8")
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "words": [
                            {
                                "start_seconds": 7.0,
                                "end_seconds": 8.0,
                                "text": "later product explanation",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            info = {
                "work_dir": str(root),
                "transcript_path": str(transcript_path),
                "transcript_words_path": str(words_path),
            }
            result = {
                "video_understanding": {
                    role: {
                        "evidence_units": [
                            {
                                "id": "B1" if role == "benchmark" else "C1",
                                "voiceover": "later product explanation",
                                "time_range": "0.0s - 2.0s",
                            }
                        ]
                    }
                    for role in ("benchmark", "creator")
                },
                "stage_analysis": [
                    {
                        "stage": "S1 Hook",
                        "benchmark_time_range": "0.0s - 2.0s",
                        "creator_time_range": "0.0s - 2.0s",
                        "benchmark_evidence_ids": ["B1"],
                        "creator_evidence_ids": ["C1"],
                    }
                ],
            }
            with self.assertRaisesRegex(SystemExit, "超出阶段时间窗口"):
                validate_transcript_attribution(
                    result,
                    {"videos": {"benchmark": info, "creator": info}},
                )

    def test_timeline_transcript_hides_coarse_full_video_segment(self) -> None:
        selected = select_timeline_transcript(
            [{"start_seconds": 0.12, "end_seconds": 50.74, "text": "full video transcript"}],
            [],
            0.0,
            6.0,
        )
        self.assertEqual(selected["scope"], "insufficient_precision")
        self.assertNotIn("full video transcript", " ".join(selected["display_lines"]))
        self.assertIn("时间粒度不足", " ".join(selected["display_lines"]))

    def test_timeline_view_loads_word_timestamps_from_video_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            frame = self._write_frame(frames_dir, "frame.jpg", (20, 30, 40))
            srt_path = root / "transcript.srt"
            srt_path.write_text(
                "1\n00:00:00,120 --> 00:00:10,000\nfull video transcript\n",
                encoding="utf-8",
            )
            words_path = root / "transcript.words.json"
            words_path.write_text(
                json.dumps(
                    {
                        "schema_version": "flayr.transcript_words.v1",
                        "words": [
                            {"start_seconds": 0.12, "end_seconds": 0.4, "text": "hook"},
                            {"start_seconds": 7.0, "end_seconds": 7.3, "text": "later"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            view = build_timeline_view_for_range(
                root,
                {
                    "work_dir": str(root),
                    "duration_seconds": 10.0,
                    "transcript_segments_path": str(srt_path),
                    "transcript_words_path": str(words_path),
                    "analysis_frames": [{"path": str(frame), "timestamp_seconds": 1.0}],
                },
                "hook",
                0.0,
                6.0,
            )
            self.assertIsNotNone(view)
            self.assertEqual(view["transcript_scope"], "word_window")
            self.assertEqual(view["transcript_word_count"], 1)

    def test_first_build_passes_canonical_manifest_to_all_downstream_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "frames" / "selected.jpg"
            canonical.parent.mkdir()
            canonical.write_bytes(b"selected")
            selection = {
                "analysis_frames": [{"path": str(canonical), "timestamp_seconds": 1.0}],
                "analysis_stage_frames": [{"path": str(canonical), "timestamp_seconds": 1.0, "stage": "S1"}],
            }
            captured: list[dict] = []

            def capture(_role_dir: Path, view_info: dict) -> dict:
                captured.append(view_info)
                return {}

            with (
                mock.patch("flayr_core.video_evidence.build_frame_selection_report", return_value=selection),
                mock.patch("flayr_core.video_evidence.build_contact_sheets", side_effect=capture),
                mock.patch("flayr_core.video_evidence.build_transcript_pack", side_effect=capture),
                mock.patch("flayr_core.video_evidence.build_timeline_views", side_effect=capture),
                mock.patch(
                    "flayr_core.video_evidence.audit_video_evidence",
                    return_value={"path": str(root / "audit.json"), "warnings": []},
                ),
            ):
                build_video_evidence_artifacts(
                    root,
                    {
                        "work_dir": str(root),
                        "frames": [{"path": str(root / "raw.jpg"), "timestamp_seconds": 1.0}],
                    },
                )

            self.assertEqual(len(captured), 3)
            for view_info in captured:
                self.assertEqual(view_info["analysis_frames"], selection["analysis_frames"])
                self.assertEqual(view_info["analysis_stage_frames"], selection["analysis_stage_frames"])
                self.assertEqual(
                    view_info["video_evidence"]["analysis_frames"],
                    selection["analysis_frames"],
                )

    def test_llm_candidates_consume_canonical_manifest_before_raw_frames(self) -> None:
        info = {
            "frames": [{"timestamp_seconds": 99, "path": "/raw.jpg"}],
            "analysis_frames": [
                {"timestamp_seconds": 5, "path": "/scene.jpg", "selection_reasons": ["scene_boundary"]},
                {"timestamp_seconds": 1, "path": "/density.jpg", "selection_reasons": ["density_floor"]},
            ],
        }
        candidates = get_llm_frame_candidates(info, 1)
        self.assertEqual([item["path"] for item in candidates], [str(Path("/scene.jpg").resolve())])

    def test_time_range_selection_uses_canonical_manifest(self) -> None:
        info = {
            "frames": [{"timestamp_seconds": 2, "path": "/raw.jpg"}],
            "analysis_frames": [{"timestamp_seconds": 2, "path": "/selected.jpg", "selection_reason": "scene_boundary"}],
            "duration_seconds": 5,
        }
        selected = select_frames_for_time_range(info, "1s - 3s", limit=1)
        self.assertEqual([item["path"] for item in selected], [str(Path("/selected.jpg").resolve())])

    def test_stage_manifest_preserves_selection_provenance(self) -> None:
        frames = [
            {"timestamp_seconds": 0, "path": "/first.jpg", "selection_reasons": ["first_frame"]},
            {"timestamp_seconds": 5, "path": "/cta.jpg", "selection_reasons": ["focus_cta"]},
        ]
        stages = build_stage_frame_manifest(frames, 5)
        self.assertTrue(any("selection_reasons" in item for item in stages))

    def test_stage_recovery_visual_inputs_only_include_requested_stages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            frames_dir.mkdir()
            s1 = self._write_frame(frames_dir, "s1.jpg", (20, 30, 40))
            s3 = self._write_frame(frames_dir, "s3.jpg", (40, 50, 60))
            s4 = self._write_frame(frames_dir, "s4.jpg", (60, 70, 80))
            info = {
                "work_dir": str(root),
                "analysis_stage_frames": [
                    {"stage": "S1", "path": str(s1), "timestamp_seconds": 1.0},
                    {"stage": "S3", "path": str(s3), "timestamp_seconds": 3.0},
                    {"stage": "S4", "path": str(s4), "timestamp_seconds": 4.0},
                ],
            }
            selected = select_stage_recovery_visual_inputs(
                info,
                "creator",
                ["S3", "malformed-stage"],
                image_limit=4,
            )
            self.assertEqual([item["path"] for item in selected], [str(s3.resolve())])
            self.assertTrue(all("Stage1-C S3" in item["label"] for item in selected))

    def test_ocr_frame_candidates_include_focus_frames_without_duplicates(self) -> None:
        info = {
            "frames": [
                {"timestamp_seconds": 0.0, "path": "/base-0.jpg"},
                {"timestamp_seconds": 4.0, "path": "/shared.jpg"},
            ],
            "focus_frames": [
                {"timestamp_seconds": 4.0, "path": "/shared.jpg", "label": "cta"},
                {"timestamp_seconds": 5.0, "path": "/focus-cta.jpg", "label": "cta"},
            ],
        }
        entries = _merge_ocr_frame_entries(info)
        self.assertEqual(
            [item["path"] for item in entries],
            [
                str(Path("/base-0.jpg").resolve()),
                str(Path("/shared.jpg").resolve()),
                str(Path("/focus-cta.jpg").resolve()),
            ],
        )

    def test_online_asr_response_is_normalized_to_word_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.json"
            path.write_text(
                json.dumps(
                    {
                        "output": {
                            "sentence": {
                                "begin_time": 1200,
                                "end_time": 2400,
                                "text": "hello world",
                                "words": [
                                    {"text": "hello", "begin_time": 1200, "end_time": 1800},
                                    {"text": "world", "begin_time": 1800, "end_time": 2400},
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                extract_word_timestamps(path),
                [
                    {"start_seconds": 1.2, "end_seconds": 1.8, "text": "hello"},
                    {"start_seconds": 1.8, "end_seconds": 2.4, "text": "world"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
