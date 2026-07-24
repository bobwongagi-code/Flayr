from __future__ import annotations

import base64
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


if __name__ == "__main__":
    unittest.main()
