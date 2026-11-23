from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.flayr_core import requirement_review as rr
from scripts.flayr_core.human_review import HumanReviewError
from scripts.flayr_core.run_manifest import write_success_manifest
from scripts.flayr_core.utils import write_json
from scripts.flayr_core import human_review
from scripts import review_requirements as cli
from tests import test_human_review as review_fixtures
from tests import test_degraded_human_review as degraded_fixtures


class RequirementReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / "run"
        self.media = self.run / "creator"
        self.media.mkdir(parents=True)
        self.video = self.root / "creator.mp4"
        self.video.write_bytes(b"fixture-video")
        self.frame = self.media / "frame.jpg"
        self.frame.write_bytes(b"fixture-frame")
        self.source = self.root / "requirements.txt"
        self.source.write_text("Show the connector.\nShow charging.\n", encoding="utf-8")
        self.requirements = self.root / "requirements.json"
        self.links = self.root / "links.json"
        self.output = self.root / "review.json"
        self.bundle = {
            "id": "shoot-v1", "version": "1", "approved_by": "operator",
            "requirements": [
                {"id": "R1", "text": "Show the connector.", "source_type": "user_provided",
                 "source_path": "requirements.txt", "source_locator": {"kind": "quote", "value": "Show the connector."}},
                {"id": "R2", "text": "Show charging.", "source_type": "user_provided",
                 "source_path": "requirements.txt", "source_locator": {"kind": "quote", "value": "Show charging."}},
            ],
        }
        self.analysis = {
            "videos": {"creator": {"path": str(self.video), "duration_seconds": 12,
                "work_dir": str(self.media), "analysis_frames": [
                    {"path": str(self.frame), "timestamp_seconds": 3},
                ]}},
            "video_understanding": {"creator": {"evidence_units": [
                {"id": "C1", "time_range": "2s - 4s", "visual_fact": "Connector shown.", "voiceover": "Type C"},
            ]}},
        }
        write_json(self.run / "analysis.json", self.analysis)
        write_json(self.requirements, self.bundle)
        write_json(self.links, {"R1": ["C1"]})
        patcher = mock.patch.object(rr, "load_analysis", side_effect=lambda _: json.loads((self.run / "analysis.json").read_text()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self):
        return rr.init_packet(self.run, self.requirements, self.links, self.output)

    def test_candidate_is_not_completion_and_empty_is_not_absence(self):
        packet = self.create()
        self.assertEqual(packet["rows"][0]["candidates"][0]["time_range"], [2, 4])
        self.assertEqual(packet["rows"][1]["candidates"], [])
        self.assertEqual(packet["human_notes"], [])
        self.assertNotIn("score", packet)
        self.assertNotIn("status", packet["rows"][0])
        self.assertIn("未提供候选不代表没做", rr.render_packet(packet))
        self.assertIn("尚未提供候选", rr.render_packet(packet))

    def test_all_in_range_frames_kept_without_semantic_ranking(self):
        second = self.media / "second.jpg"
        second.write_bytes(b"second")
        self.analysis["videos"]["creator"]["analysis_frames"].append({"path": str(second), "timestamp_seconds": 2.5})
        write_json(self.run / "analysis.json", self.analysis)
        frames = self.create()["rows"][0]["candidates"][0]["screenshots"]
        self.assertEqual([frame["seconds"] for frame in frames], [2.5, 3])

    def test_outside_window_frame_is_not_substituted(self):
        self.analysis["videos"]["creator"]["analysis_frames"][0]["timestamp_seconds"] = 9
        write_json(self.run / "analysis.json", self.analysis)
        packet = self.create()
        self.assertEqual(packet["rows"][0]["candidates"][0]["screenshots"], [])
        self.assertIn("没有已绑定截图", rr.render_packet(packet))

    def test_no_frames_keeps_text_candidate_without_claiming_visual_proof(self):
        self.analysis["videos"]["creator"]["analysis_frames"] = []
        write_json(self.run / "analysis.json", self.analysis)
        packet = self.create()
        self.assertEqual(packet["rows"][0]["candidates"][0]["observations"]["voiceover"], "Type C")
        self.assertIn("未核实", rr.render_packet(packet))

    def test_source_gate_failure_is_not_an_empty_result(self):
        with mock.patch.object(rr, "load_analysis", side_effect=HumanReviewError("bad source")):
            with self.assertRaises(HumanReviewError):
                self.create()
        self.assertFalse(self.output.exists())

    def test_unknown_and_cross_video_evidence_rejected(self):
        for candidates in (["B1"], ["missing"], ["C1", "C1"], "C1", [1], [{"id": "C1"}]):
            with self.subTest(candidates=candidates):
                write_json(self.links, {"R1": candidates})
                with self.assertRaises(rr.RequirementReviewError):
                    self.create()

    def test_unknown_requirement_and_score_fields_rejected(self):
        for mapping in ({"R9": ["C1"]}, {"R1": {"score": 3}}, {"R1": ["C1"], "completed": True}):
            write_json(self.links, mapping)
            with self.assertRaises(rr.RequirementReviewError):
                self.create()

    def test_requirement_sources_must_be_explicit_and_locatable(self):
        changes = [
            {"source_type": "model_inferred"}, {"source_path": "missing.txt"},
            {"source_locator": {"kind": "quote", "value": "Not in the source"}},
            {"source_locator": {"kind": "page", "value": 0}},
            {"source_locator": {"kind": "time_range", "value": [4, 2]}},
            {"score": 3},
        ]
        for change in changes:
            bundle = copy.deepcopy(self.bundle)
            bundle["requirements"][0].update(change)
            write_json(self.requirements, bundle)
            with self.subTest(change=change), self.assertRaises(rr.RequirementReviewError):
                self.create()

    def test_duplicate_requirement_ids_rejected(self):
        self.bundle["requirements"][1]["id"] = "R1"
        write_json(self.requirements, self.bundle)
        with self.assertRaises(rr.RequirementReviewError):
            self.create()

    def test_invalid_time_windows_not_clamped(self):
        for bounds in ([3, 2], [2, 13], [-1, 3], [True, 3], [0, float("inf")]):
            self.analysis["video_understanding"]["creator"]["evidence_units"][0]["time_range"] = bounds
            write_json(self.run / "analysis.json", self.analysis)
            with self.subTest(bounds=bounds), self.assertRaises(rr.RequirementReviewError):
                self.create()

    def test_point_evidence_keeps_exact_time_without_expanding_window(self):
        self.analysis["video_understanding"]["creator"]["evidence_units"][0]["time_range"] = [3, 3]
        write_json(self.run / "analysis.json", self.analysis)
        self.assertEqual(self.create()["rows"][0]["candidates"][0]["time_range"], [3, 3])

    def test_missing_ledger_is_not_empty_candidates(self):
        self.analysis["video_understanding"] = {}
        write_json(self.run / "analysis.json", self.analysis)
        with self.assertRaises(rr.RequirementReviewError):
            self.create()

    def test_duplicate_evidence_rejected(self):
        units = self.analysis["video_understanding"]["creator"]["evidence_units"]
        units.append(copy.deepcopy(units[0]))
        write_json(self.run / "analysis.json", self.analysis)
        with self.assertRaises(rr.RequirementReviewError):
            self.create()

    def test_screenshot_outside_creator_rejected_even_if_frame_accessor_supplies_it(self):
        other = self.run / "benchmark.jpg"
        other.write_bytes(b"wrong side")
        with mock.patch.object(rr, "get_analysis_frame_entries", return_value=[{"path": str(other), "timestamp_seconds": 3}]):
            with self.assertRaises(rr.RequirementReviewError):
                self.create()

    def test_missing_in_range_screenshot_is_not_silently_bound(self):
        with mock.patch.object(rr, "get_analysis_frame_entries", return_value=[{"path": str(self.media / "missing.jpg"), "timestamp_seconds": 3}]):
            with self.assertRaises(rr.RequirementReviewError):
                self.create()

    def test_notes_are_separate_append_only_records_not_model_results(self):
        self.create()
        before = (self.run / "analysis.json").read_bytes()
        rr.record_note(self.output, "R1", "reviewer", "Need a closer shot.")
        packet = rr.record_note(self.output, "R1", "reviewer", "Correction: this is adequate.")
        self.assertEqual(len(packet["human_notes"]), 2)
        self.assertEqual((self.run / "analysis.json").read_bytes(), before)
        self.assertNotIn("status", packet["rows"][0])
        self.assertEqual(rr.load_packet(self.output), packet)

    def test_unknown_or_empty_note_rejected(self):
        self.create()
        for identifier, name, note in (("R9", "a", "b"), ("R1", "", "b"), ("R1", "a", "")):
            with self.assertRaises(rr.RequirementReviewError):
                rr.record_note(self.output, identifier, name, note)
        self.assertEqual(rr.load_packet(self.output)["human_notes"], [])

    def test_changed_sources_invalidate_review(self):
        self.create()
        for source in (self.source, self.requirements, self.links, self.video, self.frame, self.run / "analysis.json"):
            original = source.read_bytes()
            source.write_bytes(original + b" ")
            with self.subTest(source=source), self.assertRaises(rr.RequirementReviewError):
                rr.load_packet(self.output)
            source.write_bytes(original)
        self.assertEqual(rr.load_packet(self.output)["mode"], "candidate_review_only")

    def test_candidate_tampering_rejected(self):
        packet = self.create()
        packet["rows"][0]["candidates"][0]["time_range"] = [0, 12]
        write_json(self.output, packet)
        with self.assertRaises(rr.RequirementReviewError):
            rr.load_packet(self.output)

    def test_packet_overwrite_rejected(self):
        self.create()
        with self.assertRaises(rr.RequirementReviewError):
            self.create()

    def test_no_changes_to_input_files(self):
        paths = [self.source, self.requirements, self.links, self.video, self.frame, self.run / "analysis.json"]
        before = [_hash(path) for path in paths]
        self.create()
        rr.load_packet(self.output)
        self.assertEqual(before, [_hash(path) for path in paths])

    def completed_run(self):
        helper = review_fixtures.HumanReviewTests()
        temp, run = helper.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = helper._analysis()
        media = run / "creator"
        media.mkdir()
        frame = media / "frame.jpg"
        frame.write_bytes(b"frame")
        analysis["videos"]["creator"] = {
            "path": str(Path(temp.name) / "input.mp4"), "duration_seconds": 12,
            "transcription_status": "completed",
            "work_dir": str(media), "analysis_frames": [{"path": str(frame), "timestamp_seconds": 3}],
        }
        analysis["video_understanding"]["creator"]["evidence_units"][0]["time_range"] = [2, 4]
        helper._replace_run_analysis(temp, run, analysis)
        video = Path(temp.name) / "input.mp4"
        write_success_manifest(run, {"creator_video": video}, analysis)
        return run, video

    def test_real_source_gate_and_existing_review_fixture(self):
        run, video = self.completed_run()
        with mock.patch.object(rr, "load_analysis", human_review.load_analysis):
            original = video.read_bytes()
            video.write_bytes(b"x" * len(original))
            with self.assertRaisesRegex(HumanReviewError, "creator video"):
                rr.init_packet(run, self.requirements, self.links, self.output)
            self.assertFalse(self.output.exists())
            video.write_bytes(original)
            rr.init_packet(run, self.requirements, self.links, self.output)
            rr.record_note(self.output, "R1", "operator", "Candidate needs visual review.")
            self.assertEqual(len(rr.load_packet(self.output)["human_notes"]), 1)
            before = self.output.read_bytes()
            video.write_bytes(b"x" * len(original))
            with self.assertRaises(HumanReviewError):
                rr.record_note(self.output, "R1", "operator", "Wrong revision")
            self.assertEqual(self.output.read_bytes(), before)
            video.write_bytes(original)
            (run / "analysis.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(HumanReviewError):
                rr.load_packet(self.output)

    def test_two_cli_processes_preserve_both_notes(self):
        run, _ = self.completed_run()
        with mock.patch.object(rr, "load_analysis", human_review.load_analysis):
            rr.init_packet(run, self.requirements, self.links, self.output)
        # Force a long read/write interval so the old unlocked implementation
        # read the same packet twice. The locks must work across interpreters.
        script = """
import sys
import time
from pathlib import Path
from scripts import review_requirements as cli
from scripts.flayr_core import requirement_review as rr
original = rr.load_packet
def delayed_read(path):
    result = original(path)
    time.sleep(1)
    return result
rr.load_packet = delayed_read
Path(sys.argv[3]).touch()
while not Path(sys.argv[4]).exists():
    time.sleep(0.01)
raise SystemExit(cli.main(["note", sys.argv[1], "--requirement-id", "R1",
                          "--reviewer", sys.argv[2], "--note", sys.argv[2]]))
"""
        children = []
        try:
            barrier = self.root / "start"
            for name in ("one", "two"):
                children.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(self.output), name,
                     str(self.root / name), str(barrier)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                ))
            stop = time.monotonic() + 15
            while not all((self.root / name).exists() for name in ("one", "two")):
                self.assertLess(time.monotonic(), stop, "note workers did not start")
                time.sleep(0.01)
            barrier.touch()
            for child in children:
                _, error = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, error)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        with mock.patch.object(rr, "load_analysis", human_review.load_analysis):
            notes = rr.load_packet(self.output)["human_notes"]
        self.assertCountEqual([note["note"] for note in notes], ["one", "two"])

    def test_valid_degraded_review_needs_no_success_manifest(self):
        fixture = degraded_fixtures.DegradedHumanReviewTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.analysis["mode"] = "improve"
        fixture.analysis["videos"]["creator"]["duration_seconds"] = 12
        fixture.analysis["video_understanding"] = {"creator": {"evidence_units": [
            {"id": "C1", "time_range": [2, 4], "visual_fact": "Fixture evidence"},
        ]}}
        write_json(fixture.root / "analysis.json", fixture.analysis)
        with mock.patch.object(rr, "load_analysis", human_review.load_analysis):
            packet = rr.init_packet(fixture.root, self.requirements, self.links, self.output)
        self.assertEqual(packet["rows"][0]["candidates"][0]["evidence_id"], "C1")
        self.assertFalse((fixture.root / "_SUCCESS.json").exists())

    def test_cli_round_trip_and_failure_exit(self):
        with mock.patch.object(cli, "init_packet", rr.init_packet), \
                mock.patch.object(cli, "load_packet", rr.load_packet), \
                mock.patch.object(cli, "record_note", rr.record_note), \
                mock.patch.object(cli, "HumanReviewError", HumanReviewError), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(["init", str(self.run), "--requirements", str(self.requirements),
                                       "--links", str(self.links), "--output", str(self.output)]), 0)
            self.assertEqual(cli.main(["note", str(self.output), "--requirement-id", "R1",
                                       "--reviewer", "operator", "--note", "Check connector visibility."]), 0)
            self.assertEqual(cli.main(["show", str(self.output)]), 0)
            self.assertIn("Check connector visibility.", output.getvalue())
            self.source.write_text("changed", encoding="utf-8")
            self.assertEqual(cli.main(["show", str(self.output)]), 2)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
