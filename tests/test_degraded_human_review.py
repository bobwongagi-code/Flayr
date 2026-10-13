import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core import human_review as hr
from scripts.flayr_core.bd_report import build_bd_report_data
from scripts.flayr_core.run_manifest import BASE_REQUIRED_ARTIFACTS, REPORT_VARIANT_ARTIFACTS
from scripts.flayr_core.utils import write_json


class DegradedHumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()
        videos = {}
        for role in ("benchmark", "creator"):
            video = Path(self.temp.name) / f"{role}.mp4"
            video.write_bytes(b"original-video")
            work = self.root / role
            work.mkdir()
            frame = work / "frame.jpg"
            frame.write_bytes(b"original-frame")
            fingerprint = {"source_video": {"size_bytes": video.stat().st_size,
                                             "sha256": hashlib.sha256(video.read_bytes()).hexdigest()}}
            info = {"path": str(video), "work_dir": str(work), "transcription_status": "completed",
                    "preprocess_fingerprint": fingerprint,
                    "preprocess_artifacts": {"files": {"frame.jpg": {
                        "size_bytes": frame.stat().st_size, "sha256": hashlib.sha256(frame.read_bytes()).hexdigest()}}}}
            write_json(work / "_preprocess.json", info)
            videos[role] = info
            write_json(self.root / f"video_facts_{role}.json", {"evidence_units": []})
        self.analysis = {"mode": "compare", "analysis_run_state": "degraded", "videos": videos,
                         "product": {"name": "Test"}, "degraded_flags": ["S5 unresolved"],
                         "stage_analysis": [{"stage": f"S{i}", "analysis_status": "completed",
                                             "relation": "tie", "model_gap_magnitude": "none",
                                             "severity": "none", "judgment_reason": "Equal"}
                                            for i in range(1, 7)]}
        self.analysis["stage_analysis"][4].update(relation=None, severity=None,
                                                  model_gap_magnitude=None, analysis_status="evidence_blocked")
        for name in (*BASE_REQUIRED_ARTIFACTS, *REPORT_VARIANT_ARTIFACTS):
            (self.root / name).write_text("{}" if name.endswith(".json") else "draft")
        write_json(self.root / "analysis.json", self.analysis)
        write_json(self.root / "run_state.json", {"schema_version": 1, "state": "DEGRADED", "history": []})
        write_json(self.root / "degraded_manifest.json", {"analysis_run_state": "degraded"})
        write_json(self.root / "final_derived_result.json", {
            "postprocess_provenance": {"field_sources": {"coverage": "complete", "unresolved_paths": []}}})
        write_json(self.root / "stage1_provider_creator_A.json", {"response": "original"})

    def test_terminal_draft_has_distinct_bound_source(self):
        review = hr.init_review(self.root)
        self.assertEqual(review["source"]["source_run_state"], "DEGRADED")
        self.assertIn(str(Path("creator") / "frame.jpg"), review["source"]["review_inputs"])
        self.assertFalse((self.root / "_SUCCESS.json").exists())
        with self.assertRaises(hr.HumanReviewError):
            hr.update_stage(self.root, "S5", "confirmed")

    def test_failed_and_partial_runs_rejected(self):
        for state in ("FAILED", "PROCESSING", "COMPLETED"):
            with self.subTest(state=state):
                write_json(self.root / "run_state.json", {"schema_version": 1, "state": state, "history": []})
                with self.assertRaises(hr.HumanReviewError):
                    hr.init_review(self.root)

    def test_invalid_success_marker_cannot_fall_back(self):
        write_json(self.root / "_SUCCESS.json", {})
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_missing_artifact_rejected(self):
        (self.root / "raw_model_response.json").unlink()
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_unresolved_field_sources_rejected(self):
        write_json(self.root / "final_derived_result.json", {
            "postprocess_provenance": {"field_sources": {"coverage": "partial", "unresolved_paths": ["S5"]}}})
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_unfinished_asr_rejected(self):
        self.analysis["videos"]["creator"]["transcription_status"] = "failed"
        write_json(self.root / "analysis.json", self.analysis)
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_missing_video_role_rejected(self):
        del self.analysis["videos"]["benchmark"]
        write_json(self.root / "analysis.json", self.analysis)
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_prior_preprocess_hash_mismatch_rejected(self):
        (self.root / "creator/frame.jpg").write_bytes(b"tampered-before-review")
        with self.assertRaises(hr.HumanReviewError):
            hr.init_review(self.root)

    def test_provider_changes_invalidate_existing_review(self):
        hr.init_review(self.root)
        write_json(self.root / "stage1_provider_creator_A.json", {"response": "changed"})
        with self.assertRaises(hr.StaleHumanReviewError):
            hr.load_review(self.root)

    def test_original_video_changes_rejected(self):
        hr.init_review(self.root)
        Path(self.analysis["videos"]["creator"]["path"]).write_bytes(b"changed")
        with self.assertRaises(hr.HumanReviewError):
            hr.load_review(self.root)

    def test_reviewed_projection_preserves_original_failure(self):
        self.analysis["stage_analysis"][0].update(relation="creator_better", severity="small", model_gap_magnitude="small")
        write_json(self.root / "analysis.json", self.analysis)
        hr.init_review(self.root)
        before = hashlib.sha256((self.root / "analysis.json").read_bytes()).hexdigest()
        for i in range(1, 7):
            if i == 5:
                hr.update_stage(self.root, "S5", "corrected", relation="creator_better", gap="small", note="Visible source")
            else:
                hr.update_stage(self.root, f"S{i}", "confirmed")
        projected = hr.project_reviewed_analysis(self.root)
        self.assertEqual(projected["analysis_run_state"], "degraded")
        self.assertFalse(projected["review_delivery"]["model_completed"])
        self.assertEqual(projected["review_status"], "approved")
        report = build_bd_report_data(projected)
        self.assertEqual(report["stages"][0]["severityLabel"], "小")
        self.assertTrue(report["degraded"])
        self.assertEqual(report["reviewStatus"], "approved")
        paths = hr.write_reviewed_reports(self.root)
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(hr.load_review(self.root)["source"]["analysis_sha256"], before)
        self.assertEqual(before, hashlib.sha256((self.root / "analysis.json").read_bytes()).hexdigest())
        self.assertFalse((self.root / "_SUCCESS.json").exists())


if __name__ == "__main__":
    unittest.main()
