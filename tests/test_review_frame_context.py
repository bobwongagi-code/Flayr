from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.artifacts import select_frames_for_time_range
from flayr_core.llm.media import select_stage_recovery_visual_inputs
from flayr_core.llm.s4_visual_verifier import _collect_stage_frames
from flayr_core.video_evidence import frames_for_range


class ReviewFrameContextTests(unittest.TestCase):
    def make_info(self, root: Path) -> dict:
        frames = []
        for timestamp in (0, 26, 27, 28, 60):
            path = root / f"frame-{timestamp}.jpg"
            Image.new("RGB", (90, 120), (timestamp * 4, 30, 50)).save(path)
            frames.append({"path": str(path), "timestamp_seconds": timestamp})
        return {
            "work_dir": str(root), "duration_seconds": 60,
            "frames": frames,
            "analysis_frames": [frames[0], frames[2], frames[4]],
        }

    def test_recovery_keeps_anchor_and_raw_neighbors_without_losing_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            selected = select_stage_recovery_visual_inputs(info, "creator", ["S5"], 3)
            self.assertEqual([item["timestamp_seconds"] for item in selected], [0, 27, 60])
            context = selected[1]
            self.assertEqual(context["source_frame_timestamps"], [26, 27, 28])
            self.assertIn("28.0s", context["label"])
            with Image.open(context["path"]) as picture:
                self.assertEqual(picture.size, (270, 148))
                self.assertLess(abs(picture.getpixel((225, 80))[0] - 112), 5)
            self.assertEqual(len(selected), 3)
            self.assertEqual(select_stage_recovery_visual_inputs(info, "creator", ["S5"], 0), [])

    def test_review_reads_raw_frames_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            for selector in (
                lambda: select_frames_for_time_range(info, "26s - 28s", 3),
                lambda: frames_for_range(info, 26, 28, 3),
            ):
                self.assertEqual([item["timestamp_seconds"] for item in selector()], [26, 27, 28])

    def test_empty_window_does_not_borrow_outside_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            self.assertEqual(select_frames_for_time_range(info, "10s - 11s", 3), [])
            self.assertEqual(frames_for_range(info, 10, 11, 3), [])
            self.assertEqual(select_frames_for_time_range(info, "61s - 62s", 3), [])

    def test_one_frame_is_centered_and_canonical_duplicate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            info["analysis_frames"] = list(info["frames"])
            info["analysis_frames"][2]["selection_reason"] = "scene_boundary"
            selected = select_frames_for_time_range(info, "26s - 28s", 1)
            self.assertEqual(selected[0]["timestamp_seconds"], 27)
            self.assertEqual(selected[0]["selection_reason"], "scene_boundary")

    def test_review_accepts_valid_timestamp_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            for item in info["frames"]:
                item["timestamp_seconds"] = f"{item['timestamp_seconds']}s"
            selected = select_frames_for_time_range(info, "26s - 28s", 1)
            self.assertEqual(selected[0]["timestamp_seconds"], "27s")
            context = select_stage_recovery_visual_inputs(info, "creator", ["S5"], 3)
            self.assertEqual(context[1]["source_frame_timestamps"], [26, 27, 28])

    def test_context_does_not_read_another_role_or_escape_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            info = self.make_info(Path(tmp))
            foreign = Path(other) / "foreign.jpg"
            Image.new("RGB", (90, 120)).save(foreign)
            info["frames"] = [{"path": str(foreign), "timestamp_seconds": 28}]
            selected = select_stage_recovery_visual_inputs(info, "creator", ["S5"], 3)
            self.assertEqual(selected[1]["source_frame_timestamps"], [27])

    def test_visual_review_does_not_spend_all_slots_on_first_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = self.make_info(Path(tmp))
            result = {"video_understanding": {"creator": {"evidence_units": [
                {"id": "C1", "time_range": "26s - 28s"},
                {"id": "C2", "time_range": "60s"},
            ]}}}
            stage = {"creator_evidence_ids": ["C1", "C2"]}
            selected = _collect_stage_frames("creator", stage, result, {"videos": {"creator": info}}, "s4", 3)
            self.assertEqual(len(selected), 3)
            self.assertIn("evidence=C2", selected[1]["label"])


if __name__ == "__main__":
    unittest.main()
