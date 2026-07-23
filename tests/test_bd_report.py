from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core.bd_report import build_bd_report_data, write_bd_report


class BdReportTests(unittest.TestCase):
    def _analysis(self) -> dict:
        return {
            "analysis_run_state": "completed",
            "generated_at": "2026-07-23T12:34:56",
            "analysis_scope": {"level": "strategy"},
            "product": {"name": "测试产品", "target_market": "my"},
            "one_line_verdict": "S3 卖点演示和 S6 促单转化是本次主要差距。",
            "executive_summary": "标杆通过使用过程和明确的购买信息完成说服链路。",
            "global_diagnosis": {
                "findings": [
                    {"id": "selling_point_route", "impact": "blocking", "summary": "主卖点路线需要先处理。"},
                    {"id": "attention_cleanliness", "impact": "minor", "summary": "背景干扰较轻。"},
                ]
            },
            "video_understanding": {
                "creator": {
                    "communication_strategy": "先确认达人想重点传达什么，再讨论对应画面。",
                    "evidence_units": [
                        {"id": "C1", "information": "达人口播描述产品，但没有展示使用过程。", "voiceover": "我来介绍一下。"}
                    ],
                },
                "benchmark": {
                    "evidence_units": [
                        {"id": "B1", "information": "标杆展示了产品实际使用过程。", "visual_fact": "画面可见产品接触目标对象。"}
                    ]
                },
            },
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "severity": "small",
                    "creator_time_range": "0s - 3s",
                    "benchmark_time_range": "0s - 2s",
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                    "creator_key_message": "达人开头直接介绍产品。",
                    "benchmark_key_message": "标杆开头先建立观看理由。",
                    "gap": "两侧开场功能不同。",
                }
            ],
            "improvements": [
                {
                    "title": "补齐真实使用过程",
                    "priority": 1,
                    "gmv_impact": "高",
                    "gap_type": "execution",
                    "suggestion": "把使用动作提前到卖点出现的位置。",
                    "benchmark_reference": "参考标杆的连续使用镜头。",
                    "benchmark_time_range": "6s - 14s",
                    "benchmark_evidence_ids": ["B1"],
                },
                {"title": "第二项", "priority": 2, "gmv_impact": "中", "gap_type": "structural", "suggestion": "第二项动作。"},
                {"title": "第三项", "priority": 3, "gmv_impact": "低", "gap_type": "resource", "suggestion": "第三项动作。"},
                {"title": "第四项不应展示", "priority": 4, "gmv_impact": "高", "gap_type": "execution", "suggestion": "第四项动作。"},
            ],
        }

    def test_projection_matches_internal_report_contract(self) -> None:
        data = build_bd_report_data(self._analysis())
        self.assertEqual(data["title"], "测试产品 · 提升报告")
        self.assertEqual(data["market"], "马来西亚")
        self.assertTrue(data["strategyLevel"])
        self.assertEqual(data["gates"][0]["level"], "P0")
        self.assertEqual(data["stages"][0]["severityLabel"], "小")
        self.assertIn("口播", data["stages"][0]["creator"]["text"])
        self.assertEqual(len(data["improvements"]), 3)
        self.assertEqual(data["improvements"][0]["gmvClass"], "gmv-high")
        self.assertEqual(data["improvements"][0]["gapType"], "执行性")
        self.assertIn("标杆片段 6s - 14s", data["improvements"][0]["planB"])

    def test_template_is_runtime_bound_and_escapes_script_data(self) -> None:
        template = Path("assets/bd_report.html").read_text(encoding="utf-8")
        self.assertIn("{{bd_report_data}}", template)
        self.assertIn("跟达人怎么说", template)
        self.assertIn("TOP 3 提升点 · 按 GMV 影响排序", template)
        self.assertNotIn("孩子不肯刷牙？", template)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            analysis = self._analysis()
            analysis["one_line_verdict"] = "</script><script>alert('x')</script>"
            path = write_bd_report(run_dir, analysis)
            html = path.read_text(encoding="utf-8")
            self.assertEqual(path.name, "bd_report.html")
            self.assertNotIn("{{bd_report_data}}", html)
            self.assertNotIn("</script><script>", html)
            self.assertIn("\\u003c/script\\u003e", html)
            json.loads(html.split("var report = ", 1)[1].split(";\n", 1)[0])

    def test_degraded_stages_do_not_render_default_severity(self) -> None:
        analysis = self._analysis()
        analysis["analysis_run_state"] = "degraded"
        analysis["stage_analysis"][0]["severity"] = "medium"

        data = build_bd_report_data(analysis)

        self.assertEqual(data["stages"][0]["severityLabel"], "未分析")
        self.assertEqual(data["stages"][0]["severityClass"], "sev-small")


if __name__ == "__main__":
    unittest.main()
