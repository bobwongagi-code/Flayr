"""Focused tests for local audio and windowed frame extraction."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core import video


class VideoExtractionTests(unittest.TestCase):
    def test_anchor_frames_never_replace_unverified_pts_with_requested_time(self) -> None:
        cases = (("", None), ("[showinfo] pts_time:9.0\n", None), ("[showinfo] pts_time:5.1\n", 5.1))
        for stderr, expected in cases:
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as tmp:
                result = {"duration_seconds": 12.0, "errors": [], "frames": []}
                with (
                    mock.patch.object(Path, "is_file", return_value=True),
                    mock.patch.object(video, "_collect_anchor_times", return_value=[(5.0, "scene_boundary")]),
                    mock.patch.object(video, "run_command", return_value=SimpleNamespace(returncode=0, stderr=stderr)),
                    mock.patch.object(video, "_reserve_artifacts", return_value=True),
                    mock.patch.object(video, "write_json"),
                ):
                    video.extract_anchor_frames(Path("source.mp4"), Path(tmp), result)
                self.assertEqual(result["frames"][0]["timestamp_seconds"], expected)
                self.assertEqual(result["frames"][0]["requested_timestamp_seconds"], 5.0)
                self.assertEqual(bool(result["errors"]), expected is None)

    def test_audio_stream_index_uses_only_unambiguous_ffmpeg_mapping(self) -> None:
        cases = (
            ("  Stream #0:3 -> #0:0 (aac -> pcm_s16le)\n", 3),
            ("", None),
            (
                (
                    "Stream #0:1 -> #0:0 (aac -> pcm_s16le)\n"
                    "Stream #0:2 -> #0:1 (aac -> pcm_s16le)\n"
                ),
                None,
            ),
            ("command output exceeded 1048576 bytes\nStream #0:4 -> #0:0 (...)\n", None),
        )
        for stderr, expected_index in cases:
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as tmp:
                audio_path = Path(tmp) / "audio.wav"
                commands: list[list[str]] = []

                def fake_run(
                    command: list[str],
                    *,
                    _stderr: str = stderr,
                    _commands: list[list[str]] = commands,
                    **_kwargs: object,
                ) -> SimpleNamespace:
                    _commands.append(command)
                    Path(command[-1]).write_bytes(b"wav")
                    return SimpleNamespace(returncode=0, stderr=_stderr)

                result: dict[str, object] = {
                    "duration_seconds": 12.0,
                    "errors": [],
                    "audio_stream_index": 99,
                }
                with mock.patch.object(video, "run_command", side_effect=fake_run):
                    video.extract_audio(Path("source.mp4"), audio_path, result)

                self.assertEqual(result["audio_stream_index"], expected_index)
                self.assertEqual(len(commands), 1)
                command = commands[0]
                self.assertNotIn("-map", command)
                self.assertEqual(command[command.index("-loglevel") + 1], "info")
                self.assertIn("-nostats", command)

    def test_focus_frame_window_offsets_clip_timestamps_to_source_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            focus_dir = root / "focus_frames"
            focus_dir.mkdir()
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
                commands.append(command)
                output = Path(command[-1].replace("%04d", "0001"))
                output.write_bytes(b"frame")
                local_timestamp = "0.0" if "hook_" in command[-1] else "0.5"
                return SimpleNamespace(returncode=0, stderr=f"[showinfo] pts_time:{local_timestamp}\n")

            result: dict[str, object] = {"duration_seconds": 12.0, "errors": []}
            with mock.patch.object(video, "run_command", side_effect=fake_run):
                video.extract_focus_frames(Path("source.mp4"), focus_dir, result)

            self.assertEqual(len(commands), 2)
            cta_command = commands[1]
            self.assertLess(cta_command.index("-ss"), cta_command.index("-i"))
            self.assertEqual(cta_command[cta_command.index("-ss") + 1], "7.000")
            self.assertEqual(cta_command[cta_command.index("-t") + 1], "5.000")
            frames = result["focus_frames"]
            self.assertEqual(
                [(item["label"], item["timestamp_seconds"]) for item in frames],
                [("hook", 0.0), ("cta", 7.5)],
            )

    def test_missing_or_truncated_showinfo_timestamps_remain_unknown(self) -> None:
        cases = ("", "[showinfo] pts_time:0.0\n[showinfo] pts_time:")
        for stderr in cases:
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as tmp:
                focus_dir = Path(tmp) / "focus_frames"
                focus_dir.mkdir()

                def fake_run(
                    command: list[str],
                    *,
                    _stderr: str = stderr,
                    **_kwargs: object,
                ) -> SimpleNamespace:
                    pattern = command[-1]
                    for index in (1, 2):
                        Path(pattern.replace("%04d", f"{index:04d}")).write_bytes(b"frame")
                    return SimpleNamespace(returncode=0, stderr=_stderr)

                result: dict[str, object] = {"duration_seconds": 12.0, "errors": []}
                with mock.patch.object(video, "run_command", side_effect=fake_run):
                    video.extract_focus_frames(Path("source.mp4"), focus_dir, result)

                self.assertTrue(result["focus_frames"])
                self.assertTrue(all(item["timestamp_seconds"] is None for item in result["focus_frames"]))


if __name__ == "__main__":
    unittest.main()
