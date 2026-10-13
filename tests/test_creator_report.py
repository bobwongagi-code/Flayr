from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core.creator_report import build_creator_report_data, write_creator_report
from scripts.flayr_core.report import ReportAssetContext


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class CreatorReportTests(unittest.TestCase):
    def _analysis(self, run_dir: Path) -> dict:
        frame = run_dir / "creator" / "frames" / "frame_0001.png"
        frame.parent.mkdir(parents=True)
        frame.write_bytes(ONE_PIXEL_PNG)
        return {
            "analysis_run_state": "completed",
            "product": {"name": "测试产品"},
            "videos": {
                "creator": {
                    "duration_seconds": 4,
                    "frames": [{"timestamp_seconds": 0, "path": str(frame)}],
                }
            },
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": "0s - 3s",
                            "visual_fact": "开头展示产品使用过程。",
                            "voiceover": "我来试试看。",
                        }
                    ]
                },
                "benchmark": {
                    "evidence_units": [
                        {"id": "B1", "information": "同类参考仅用于测试，不应自动展示。"}
                    ]
                },
            },
            "highlights": [{"timestamp": "00:00", "text": "开头直接进入使用过程。"}],
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_time_range": "0s - 3s",
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                    "creator_summary": "开头展示产品使用过程。",
                    "severity": "large",
                    "linked_experiment_id": "exp1",
                }
            ],
            "improvements": [
                {
                    "experiment_id": "exp1",
                    "title": "先展示使用过程",
                    "target_stage": "S1",
                    "problem": "使用过程出现得较晚。",
                    "suggestion": "把使用过程放到开头。",
                    "creator_script_zh": "开头先让大家看到使用过程。",
                    "verification": "使用过程是否在前 5 秒内出现。",
                    "severity": "large",
                    "gmv_impact": "high",
                }
            ],
        }

    def test_template_keeps_prototype_surface_and_uses_runtime_data(self) -> None:
        template = Path("assets/creator_report.html").read_text(encoding="utf-8")
        self.assertIn("--paper:#FAFAF8", template)
        self.assertIn("这条视频，我们一起复盘", template)
        self.assertIn("完整证据地图", template)
        self.assertIn("部分分析能力降级", template)
        self.assertIn("{{creator_report_data}}", template)
        self.assertNotIn("孩子不肯刷牙？", template)

    def test_projection_hides_internal_fields_and_rejects_outside_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            serialized = json.dumps(data, ensure_ascii=False)
            self.assertEqual(data["experiments"][0]["id"], "exp1")
            self.assertEqual(data["stages"][0]["linked"], "exp1")
            self.assertIsNotNone(data["stages"][0]["frame"])
            self.assertIsNone(data["stages"][0]["reference"])
            self.assertNotIn("gmv_impact", serialized)
            self.assertNotIn('"severity"', serialized)
            self.assertEqual(data["metadata"]["report_schema_version"], 2)
            self.assertEqual(data["metadata"]["template_version"], "creator-v2")
            self.assertTrue(data["metadata"]["generated_by"])

            outside = root / "outside.png"
            outside.write_bytes(ONE_PIXEL_PNG)
            analysis["videos"]["creator"]["frames"][0]["path"] = str(outside)
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertIsNone(data["stages"][0]["frame"])

    def test_report_writes_creator_artifact_and_handles_empty_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            analysis = self._analysis(run_dir)
            analysis["improvements"] = []
            analysis["stage_analysis"][0]["creator_summary"] = "待基于关键帧和转录补充。"
            path = write_creator_report(run_dir, analysis)
            html = path.read_text(encoding="utf-8")
            self.assertEqual(path.name, "creator_report.html")
            self.assertNotIn("{{creator_report_data}}", html)
            self.assertIn('"experiments":[]', html)
            self.assertIn("下一次可以试试看的方向", html)
            self.assertIn('"report_schema_version":2', html)

    def test_reviewed_stage_statuses_show_na_and_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["review_status"] = "approved"
            analysis["stage_analysis"] = [
                {"stage": "S1 Hook", "review_decision": "not_applicable", "creator_summary": "旧摘要"},
                {"stage": "S2 Promise", "review_decision": "insufficient_evidence", "creator_summary": "旧摘要"},
            ]
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertEqual(data["stages"][0]["status"], "not_applicable")
            self.assertEqual(data["stages"][0]["statusLabel"], "未纳入比较")
            self.assertEqual(data["stages"][0]["observation"], "未纳入比较")
            self.assertEqual(data["stages"][1]["status"], "insufficient_evidence")
            self.assertEqual(data["stages"][1]["statusLabel"], "证据不足，无法比较")
            self.assertEqual(data["stages"][1]["observation"], "证据不足，无法比较")

    def test_reviewed_action_shows_only_the_human_approved_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["improvements"] = [
                {
                    "target_stage": "S4",
                    "status": "human_approved_action",
                    "suggestion": "补拍真实充电过程，不用动画证明快充。",
                    "observation": "旧模型误以为动画证明了快充。",
                    "expected_effect": "预计点击率提升10%。",
                }
            ]

            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))

            self.assertEqual(len(data["experiments"]), 1)
            action = data["experiments"][0]
            self.assertEqual(action["status"], "human_approved_action")
            self.assertEqual(action["action"], "可以补拍真实充电过程，不用动画证明快充。")
            self.assertEqual(action["observation"], "")
            self.assertEqual(action["hypothesis"], "")
            self.assertNotIn("预计点击率提升", json.dumps(data, ensure_ascii=False))

    def test_reviewed_improvement_is_visible_beside_candidate_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["candidate_experiments"] = [
                {
                    "target_stage": "S4",
                    "status": "needs_human_confirmation",
                    "action": "旧候选动作",
                }
            ]
            analysis["improvements"] = [
                {
                    "target_stage": "S4",
                    "status": "human_approved_action",
                    "suggestion": "补拍真实充电过程。",
                }
            ]

            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))

            self.assertEqual([item["action"] for item in data["experiments"]], ["可以补拍真实充电过程。"])

    def test_delivery_wording_is_scoped_and_keeps_literal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            quote = "你看这个迷你产品，达人也可以用。"
            analysis["video_understanding"]["creator"]["evidence_units"][0]["voiceover"] = quote
            analysis["stage_analysis"][0].update({
                "creator_summary": "达人展示了迷你产品，但还无法确认效果。",
                "reference_observation": "标杆视频清楚展示接口。",
            })
            analysis["retained_points"] = "素材路径 /tmp/benchmark.mp4 和 creator.png 保留；`creator` 为原文件标识。"
            analysis["improvements"][0]["suggestion"] = "建议你补拍接口近景，字幕可写“你看接口”。"
            original = copy.deepcopy(analysis)

            path = write_creator_report(root / "run", analysis)
            html = path.read_text(encoding="utf-8")
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))

            self.assertEqual(analysis, original)
            self.assertEqual(data["stages"][0]["quote"], quote)
            self.assertEqual(data["stages"][0]["observation"], "出镜者展示了迷你产品，但还无法确认效果。")
            self.assertEqual(data["stages"][0]["reference"]["text"], "参考视频清楚展示接口。")
            self.assertEqual(data["experiments"][0]["action"], "可以补拍接口近景，字幕可写“你看接口”。")
            self.assertNotIn("建议你", html)
            self.assertNotIn("帮你", html)
            self.assertNotIn("达人展示", html)
            self.assertIn(quote, html)
            self.assertNotIn("卖爆", html)
            self.assertEqual(data["keep"], analysis["retained_points"])
            analysis["stage_analysis"][0]["creator_summary"] = "达人是专业化妆师。"
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertEqual(data["stages"][0]["observation"], "出镜者是专业化妆师。")

    def test_reviewed_delivery_leads_with_approved_action_not_internal_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["review_delivery"] = {"summary_status": "replaced_after_stage_review"}
            analysis["review_summary"] = {"verdict": "功能与使用：达人更强（小差距）", "detail": "内部审核说明"}
            analysis["improvements"] = [{
                "target_stage": "S4", "status": "human_approved_action",
                "suggestion": "可以补拍真实清洗前后画面。",
            }]
            original = copy.deepcopy(analysis)
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertEqual(data["intent"], "可以补拍真实清洗前后画面。")
            self.assertEqual(analysis, original)
            self.assertNotIn("可以可以", data["intent"])
            self.assertNotIn("更强", json.dumps(data["reviewSummary"], ensure_ascii=False))

            analysis["improvements"] = []
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertEqual(data["intent"], "阶段结论已确认；拍摄建议仍需单独确认。")
            self.assertEqual(data["experiments"], [])

    def test_corrected_stage_uses_review_note_not_unreviewed_model_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["stage_analysis"][0].update({
                "review_decision": "corrected",
                "review_note": "达人展示了 CCC 标志；不确认认证真实性。",
                "creator_summary": "creator资格未知，无法判断。",
            })
            data = build_creator_report_data(analysis, ReportAssetContext(root / "run"))
            self.assertEqual(data["stages"][0]["observation"], "出镜者展示了 CCC 标志；不确认认证真实性。")
            self.assertNotIn("资格未知", data["stages"][0]["observation"])

    def test_ambiguous_direct_address_requires_wording_review_not_blind_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = self._analysis(root / "run")
            analysis["improvements"][0]["suggestion"] = "如果你觉得合适，下次试试近景。"
            with self.assertRaisesRegex(ValueError, "对外交付文字"):
                write_creator_report(root / "run", analysis)
            self.assertFalse((root / "run" / "creator_report.html").exists())


if __name__ == "__main__":
    unittest.main()
