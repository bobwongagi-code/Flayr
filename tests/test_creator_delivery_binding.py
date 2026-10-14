"""Zero-call adversarial checks for author wording and evidence bindings."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core.creator_report import build_creator_report_data, write_creator_report
from scripts.flayr_core.report import ReportAssetContext
from scripts.flayr_core.stage_evidence_contracts import STAGE_EVIDENCE_CONTRACT_VERSION
from tests.test_creator_report import ONE_PIXEL_PNG


class CreatorDeliveryBindingTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict:
        videos, understanding = {}, {}
        for role, evidence_id, time_range, timestamps in (
            ("creator", "C1", "2s - 4s", [1, 3, 19]),
            ("benchmark", "B1", "16s - 20s", [2, 18, 25]),
        ):
            directory = root / role / "frames"
            directory.mkdir(parents=True)
            frames = []
            for timestamp in timestamps:
                path = directory / f"frame-{timestamp}.png"
                path.write_bytes(ONE_PIXEL_PNG)
                frames.append({"path": str(path), "timestamp_seconds": timestamp})
            videos[role] = {"duration_seconds": 30, "frames": frames, "work_dir": str(root / role)}
            understanding[role] = {"evidence_units": [{
                "id": evidence_id, "time_range": time_range,
                "visual_fact": "近景展示充电接口。", "voiceover": "接口在这里。",
            }]}
        return {
            "analysis_run_state": "completed", "product": {"name": "便携充电器"},
            "videos": videos, "video_understanding": understanding,
            "stage_analysis": [{
                "stage": "S3 功能与使用", "severity": "medium", "relation": "benchmark_better",
                "creator_evidence_ids": ["C1"], "benchmark_evidence_ids": ["B1"],
                "creator_time_range": "0s - 30s", "benchmark_time_range": "0s - 30s",
                "creator_summary": "达人展示接口。", "reference_observation": "标杆展示了五种设备。",
            }],
            "improvements": [{
                "target_stage": "S3", "status": "human_approved_action",
                "suggestion": "可以补拍接口近景。",
            }],
        }

    def project(self, root: Path, analysis: dict) -> dict:
        return build_creator_report_data(analysis, ReportAssetContext(root))

    def test_both_sides_use_their_own_evidence_windows_and_actual_frame_times(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            original = copy.deepcopy(analysis)
            data = self.project(root, analysis)
            stage = data["stages"][0]
            own = stage["creatorEvidence"][0]
            reference = stage["reference"]["evidence"][0]
            self.assertEqual(stage["name"], "功能与使用")
            self.assertEqual(own["evidenceId"], "C1")
            self.assertEqual(reference["evidenceId"], "B1")
            self.assertEqual(own["frameTs"], "3.0s")
            self.assertEqual(reference["frameTs"], "18.0s")
            self.assertEqual((own["figure"], reference["figure"]), ("图 1", "图 2"))
            self.assertEqual(data["experiments"][0]["evidenceFigures"], ["图 1", "图 2"])
            self.assertNotIn("五种设备", json.dumps(data, ensure_ascii=False))
            self.assertEqual(analysis, original)
            self.assertEqual(analysis["stage_analysis"][0]["severity"], "medium")
            self.assertNotIn("frame", stage)  # No duplicate embedded image in a legacy alias.

    def test_missing_unknown_or_invalid_reference_never_borrows_a_stage_frame(self):
        for change in ("missing_time", "unknown_id", "outside_window", "cross_role", "symlink", "bad_image", "unqualified"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                analysis = self.fixture(root)
                unit = analysis["video_understanding"]["benchmark"]["evidence_units"][0]
                unit["voiceover"] = ""
                info = analysis["videos"]["benchmark"]
                if change == "missing_time":
                    unit["time_range"] = ""
                elif change == "unknown_id":
                    analysis["stage_analysis"][0]["benchmark_evidence_ids"] = ["B999"]
                elif change == "outside_window":
                    info["frames"] = [info["frames"][0], info["frames"][2]]
                elif change == "cross_role":
                    info["frames"] = [{**analysis["videos"]["creator"]["frames"][1], "timestamp_seconds": 18}]
                elif change == "symlink":
                    path = root / "benchmark" / "wrong.png"
                    path.symlink_to(analysis["videos"]["creator"]["frames"][1]["path"])
                    info["frames"] = [{"path": str(path), "timestamp_seconds": 18}]
                elif change == "bad_image":
                    Path(info["frames"][1]["path"]).write_bytes(b"not an image")
                else:
                    analysis["video_understanding"]["benchmark"]["stage_evidence_contract_version"] = STAGE_EVIDENCE_CONTRACT_VERSION
                data = self.project(root, analysis)
                self.assertIsNone(data["stages"][0]["reference"])
                self.assertEqual(data["experiments"][0]["action"], "可以补拍接口近景。")

    def test_invalid_evidence_ranges_are_not_clamped_or_assigned_default_positions(self):
        for window in ("-2s - 4s", "20s - 16s", "16s - 35s", "nan", "S3", ""):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                analysis = self.fixture(root)
                unit = analysis["video_understanding"]["benchmark"]["evidence_units"][0]
                unit["time_range"] = window
                self.assertIsNone(self.project(root, analysis)["stages"][0]["reference"])

    def test_speech_only_is_labelled_as_speech_and_cannot_prove_a_visual_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["videos"]["benchmark"]["frames"] = []
            analysis["stage_analysis"][0].update({
                "review_decision": "corrected", "review_note": "标杆画面清楚展示接口。",
            })
            data = self.project(root, analysis)
            reference = data["stages"][0]["reference"]["evidence"][0]
            self.assertEqual(reference["sourceKind"], "speech")
            self.assertIsNone(reference["frame"])
            self.assertEqual(reference["figure"], "")
            self.assertEqual(reference["text"], "")
            self.assertEqual(reference["quote"], "接口在这里。")
            self.assertEqual(data["stages"][0]["deliveryText"], "")

    def test_internal_note_remains_unchanged_and_public_text_is_a_separate_field(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            note = "人工确认：达人展示接口。S3 判 medium，不是 large。Stage2 的理由需要修正。"
            analysis["stage_analysis"][0].update({"review_decision": "corrected", "review_note": note})
            data = self.project(root, analysis)
            self.assertEqual(analysis["stage_analysis"][0]["review_note"], note)
            self.assertEqual(data["stages"][0]["deliveryText"], "")
            self.assertNotIn("review_note", data["stages"][0])
            self.assertNotIn("Stage2", json.dumps(data, ensure_ascii=False))

    def test_conditional_internal_rationale_is_not_shortened_into_an_unconditional_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["stage_analysis"][0].update({
                "review_decision": "corrected",
                "review_note": "如果只有口播，判 small。所以画面表现已经合格。",
            })
            self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "")
            analysis["stage_analysis"][0]["review_note"] = "在看得清的情况下，S3 判 small；属于真实展示。"
            self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "")

    def test_quoted_internal_labels_are_not_smuggled_into_delivery_prose(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["stage_analysis"][0].update({
                "review_decision": "corrected", "review_note": "达人说“标杆视频展示接口”。",
            })
            self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "")
            analysis["improvements"][0]["suggestion"] = "可以参考“标杆视频”的画面。"
            with self.assertRaisesRegex(ValueError, "内部用语"):
                self.project(root, analysis)

    def test_speech_only_never_supports_implicit_visual_wording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            for info in analysis["videos"].values():
                info["frames"] = []
            for note in ("你的视频是白色款，爆款视频是黑色款。", "包装完整，质感清楚。"):
                analysis["stage_analysis"][0].update({"review_decision": "corrected", "review_note": note})
                self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "")

    def test_comparison_needs_screenshots_for_both_sides_not_one_frame_and_one_quote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["videos"]["benchmark"]["frames"] = []
            analysis["stage_analysis"][0].update({
                "review_decision": "corrected", "review_note": "你的视频更好。",
            })
            self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "")

    def test_highlight_time_label_cannot_leak_internal_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["highlights"] = [{"timestamp": "S1 pending", "text": "这段表达清楚。"}]
            with self.assertRaisesRegex(ValueError, "内部用语：timestamp"):
                self.project(root, analysis)

    def test_completed_absence_uses_human_note_not_a_single_frame_as_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["stage_analysis"][0].update({
                "review_decision": "not_applicable", "review_note": "双方没有展示实际充电。",
                "creator_evidence_ids": [], "benchmark_evidence_ids": [],
            })
            stage = self.project(root, analysis)["stages"][0]
            self.assertEqual(stage["deliveryText"], "双方没有展示实际充电。")
            self.assertEqual(stage["creatorEvidence"], [])
            self.assertIsNone(stage["reference"])
            analysis["stage_analysis"][0]["review_decision"] = "insufficient_evidence"
            self.assertEqual(self.project(root, analysis)["stages"][0]["deliveryText"], "这部分暂不作具体比较。")

    def test_output_gate_checks_displayed_prose_not_quotes_or_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            quote = "S3 medium，达人说看这个。"
            analysis["video_understanding"]["creator"]["evidence_units"][0]["voiceover"] = quote
            data = self.project(root, analysis)
            self.assertEqual(data["stages"][0]["creatorEvidence"][0]["quote"], quote)
            for word in ("S6", "medium", "DEGRADED", "prediction_unavailable"):
                analysis["improvements"][0]["suggestion"] = f"可以依据 {word} 拍摄。"
                with self.subTest(word=word), self.assertRaisesRegex(ValueError, "内部用语"):
                    write_creator_report(root, analysis)
                self.assertFalse((root / "creator_report.html").exists())

    def test_forecasts_are_removed_without_changing_recorded_prices_and_certification_quotes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            quote = "RM19.9，有 CCC 认证。"
            analysis["video_understanding"]["creator"]["evidence_units"][0]["voiceover"] = quote
            analysis["improvements"][0]["suggestion"] = "可以补拍接口近景，预计销量提升10%。"
            data = self.project(root, analysis)
            self.assertNotIn("10%", data["experiments"][0]["action"])
            self.assertEqual(data["stages"][0]["creatorEvidence"][0]["quote"], quote)

    def test_script_in_raw_quote_stays_in_the_data_block_not_executable_html(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            analysis = self.fixture(root)
            analysis["video_understanding"]["creator"]["evidence_units"][0]["voiceover"] = '</script><script>window.injected=true</script>'
            html = write_creator_report(root, analysis).read_text(encoding="utf-8")
            self.assertNotIn('</script><script>window.injected=true', html)
            self.assertIn('\\u003c/script\\u003e', html)
            self.assertNotIn("addText(left, 'stage-code'", html)
            self.assertIn("renderBoundEvidence(block, '爆款视频'", html)
