import copy
import unittest

import test_human_review as fixtures
from scripts.flayr_core import human_review as hr
from scripts.flayr_core.analysis_model import AnalysisResult
from scripts.flayr_core.postprocess.repair_stages import (
    _suppress_blocked_stage_improvements,
    stabilize_improvement_priorities,
)


class SuppressedActionReviewTests(unittest.TestCase):
    def test_filtered_actions_remain_pending_without_comparison_or_forecast(self):
        stages = [
            {"stage": "S4", "comparison_status": "not_applicable", "stage_evidence_gate": {"status": "not_applicable"}},
            {"stage": "S6", "severity": "small"},
        ]
        result = {"stage_analysis": copy.deepcopy(stages), "improvements": [
            {"title": "补充效果", "target_stage": "S4", "actions": ["补拍真实充电过程。"],
             "problem": "标杆效果更好", "gmv_reason": "提升销量15%"},
            {"title": "改善CTA", "target_stage": "S6", "suggestion": "回顾卖点后明确提醒下单。"},
        ]}
        _suppress_blocked_stage_improvements(result)
        stabilize_improvement_priorities(result)
        self.assertEqual(result["improvements"], [])
        self.assertEqual(result["stage_analysis"], stages)
        self.assertEqual(len(result["candidate_experiments"]), 2)
        for item in result["candidate_experiments"]:
            self.assertEqual(item["status"], "needs_human_confirmation")
            self.assertNotIn("problem", item)
            self.assertNotIn("gmv_reason", item)
        before = copy.deepcopy(result)
        _suppress_blocked_stage_improvements(result)
        stabilize_improvement_priorities(result)
        self.assertEqual(result, before)

    def test_provider_cannot_grant_human_approval_through_projection(self):
        source = {"candidate_experiments": [{"status": "human_approved_action", "action": "拍摄。"}]}
        output = {}
        AnalysisResult.from_mapping(source).project_into(output)
        self.assertEqual(output["candidate_experiments"][0]["status"], "needs_human_confirmation")
        self.assertEqual(source["candidate_experiments"][0]["status"], "human_approved_action")

    def pending_run(self):
        fixture = fixtures.HumanReviewTests()
        analysis = fixture._valid_comparison_analysis()
        analysis["candidate_experiments"] = [{
            "title": "S4 动作候选（待人工确认）", "target_stage": "S4",
            "status": "needs_human_confirmation", "actions": ["补拍真实充电过程。"],
        }]
        fixture._analysis = lambda: copy.deepcopy(analysis)
        temp, run = fixture.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run)
        return run, analysis

    def test_pending_actions_require_completed_stage_review(self):
        run, _ = self.pending_run()
        self.assertEqual(hr.reviewable_suggestion_actions(run), [])
        with self.assertRaises(hr.HumanReviewError):
            hr.update_suggestion_review(run, "candidate_experiments:0", "approved")

    def test_unchanged_stages_can_release_preexisting_pending_action(self):
        run, analysis = self.pending_run()
        for code in hr.STAGE_CODES:
            hr.update_stage(run, code, "confirmed")
        candidates = hr.reviewable_suggestion_actions(run)
        self.assertEqual([item["key"] for item in candidates], ["candidate_experiments:0"])
        projected = hr.project_reviewed_analysis(run)
        self.assertEqual(projected["candidate_experiments"][0]["status"], "needs_human_confirmation")
        self.assertFalse(any(item.get("status") == "human_approved_action" for item in projected["improvements"]))
        hr.update_suggestion_review(run, "candidate_experiments:0", "edited", action="补拍设备实际充电的连续画面。")
        projected = hr.project_reviewed_analysis(run)
        self.assertEqual(projected["executive_summary"], analysis["executive_summary"])
        self.assertEqual(projected["review_delivery"]["changed_stages"], [])
        item = projected["candidate_experiments"][0]
        self.assertEqual(item["status"], "human_approved_action")
        self.assertEqual(item["action"], "补拍设备实际充电的连续画面。")
        self.assertEqual(projected["improvements"][-1]["suggestion"], item["action"])
        from scripts.flayr_core.bd_report import build_bd_report_data
        from scripts.flayr_core.creator_report import build_creator_report_data
        from scripts.flayr_core.report import ReportAssetContext
        self.assertIn(item["action"], str(build_bd_report_data(projected)))
        self.assertIn(item["action"], str(build_creator_report_data(projected, ReportAssetContext(run))))
        self.assertEqual(hr.load_analysis(run), analysis)


if __name__ == "__main__":
    unittest.main()
