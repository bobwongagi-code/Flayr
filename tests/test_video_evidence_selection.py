from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.artifacts import build_stage_frame_manifest, select_frames_for_time_range  # noqa: E402
from flayr_core.frame_selection import build_analysis_frame_manifest  # noqa: E402
from flayr_core.llm.media import get_llm_frame_candidates  # noqa: E402
from flayr_core.subtitle_track import _merge_ocr_frame_entries  # noqa: E402
from flayr_core.asr import extract_word_timestamps  # noqa: E402


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

    def test_llm_candidates_consume_canonical_manifest_before_raw_frames(self) -> None:
        info = {
            "frames": [{"timestamp_seconds": 99, "path": "/raw.jpg"}],
            "analysis_frames": [
                {"timestamp_seconds": 5, "path": "/scene.jpg", "selection_reasons": ["scene_boundary"]},
                {"timestamp_seconds": 1, "path": "/density.jpg", "selection_reasons": ["density_floor"]},
            ],
        }
        candidates = get_llm_frame_candidates(info, 1)
        self.assertEqual([item["path"] for item in candidates], ["/scene.jpg"])

    def test_time_range_selection_uses_canonical_manifest(self) -> None:
        info = {
            "frames": [{"timestamp_seconds": 2, "path": "/raw.jpg"}],
            "analysis_frames": [{"timestamp_seconds": 2, "path": "/selected.jpg", "selection_reason": "scene_boundary"}],
            "duration_seconds": 5,
        }
        selected = select_frames_for_time_range(info, "1s - 3s", limit=1)
        self.assertEqual([item["path"] for item in selected], ["/selected.jpg"])

    def test_stage_manifest_preserves_selection_provenance(self) -> None:
        frames = [
            {"timestamp_seconds": 0, "path": "/first.jpg", "selection_reasons": ["first_frame"]},
            {"timestamp_seconds": 5, "path": "/cta.jpg", "selection_reasons": ["focus_cta"]},
        ]
        stages = build_stage_frame_manifest(frames, 5)
        self.assertTrue(any("selection_reasons" in item for item in stages))

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
        self.assertEqual([item["path"] for item in entries], ["/base-0.jpg", "/shared.jpg", "/focus-cta.jpg"])

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
